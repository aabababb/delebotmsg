from telethon.tl.types import (
    InputPeerUser, InputPeerChannel, MessageEntityMentionName,
    MessageService, PeerUser, Channel, ChannelParticipantsAdmins
)
from telethon import TelegramClient, sync, events, errors
from telethon.errors import RPCError
from telethon.sessions import StringSession
import sys, asyncio
import queue, time, json, os, re, traceback
from datetime import datetime, timezone, timedelta
import threading
import http.server
import collections
from urllib.parse import urlparse, parse_qs

# ========== 全局日志缓冲区 ==========
log_buffer = collections.deque(maxlen=15)
log_lock = threading.Lock()

def log(msg):
    """打印并记录日志，自动添加北京时间戳"""
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(timezone.utc).astimezone(beijing_tz).strftime('%Y-%m-%d %H:%M:%S')
    full_msg = f"[{now}] {msg}"
    with log_lock:
        log_buffer.append(full_msg)
    print(full_msg)


class TelegramBotMonitor:
    def __init__(self, config_file):
        self.config_file = config_file
        self.client = None
        self.config = self.load_config()
        # 限制同时处理的机器人消息数，避免高并发时资源耗尽
        self.semaphore = asyncio.Semaphore(5)

    def load_config(self):
        if not os.path.exists(self.config_file):
            log(f"配置文件不存在: {self.config_file}")
            return {"bots": [], "keywords": []}

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                if self.config_file.endswith('.json'):
                    return json.load(f)
        except Exception as e:
            log(f"加载配置文件失败: {e}")
            return {"bots": [], "keywords": []}

    def get_beijing_time(self, dt=None):
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing_tz = timezone(timedelta(hours=8))
        beijing_time = dt.astimezone(beijing_tz)
        return beijing_time.strftime('%Y-%m-%d %p %H:%M:%S')

    def get_bots_list(self):
        if "bots" in self.config:
            return self.config["bots"]
        bots = []
        for key, value in self.config.items():
            if "bot" in key.lower():
                bots.append(value)
        return bots

    def get_keywords_list(self):
        if "keywords" in self.config:
            return self.config["keywords"]
        keywords = []
        for key, value in self.config.items():
            if "str" in key.lower() or "keyword" in key.lower():
                keywords.append(value)
        return keywords

    async def initialize_client(self):
        """使用 StringSession 初始化客户端，不再需要交互式登录"""
        try:
            api_id = self.config.get("api_id")
            api_hash = self.config.get("api_hash")
            string_session = self.config.get("string_session")

            if not string_session:
                raise Exception("配置文件中缺少 string_session 字段，请提供有效的 StringSession")

            self.client = TelegramClient(
                StringSession(string_session),
                api_id,
                api_hash
            )

            await self.client.connect()

            if not await self.client.is_user_authorized():
                raise Exception("StringSession 无效或已过期，请更新配置文件中的 string_session")

            log("客户端初始化成功，开始监听消息...")
            return True

        except Exception as e:
            log(f"初始化客户端失败: {e}")
            return False

    async def should_delete_message(self, event):
        try:
            sender = await event.get_sender()
            if not sender or not hasattr(sender, 'username') or not sender.username:
                return False, None, 0

            message_text = event.message.text or event.message.raw_text or ""
            sender_username = sender.username.lower()

            bots = self.get_bots_list()
            keywords = self.get_keywords_list()

            is_target_bot = any(bot.lower() == sender_username for bot in bots)

            has_keyword = any(keyword.lower() in message_text.lower() for keyword in keywords)

            if not has_keyword and event.message.entities:
                has_keyword = await self.check_mentions_for_keywords(event, keywords)

            if "bot" in sender_username and has_keyword:
                return True, "bot_with_keyword", 3
            elif "bot" in sender_username and sender_username not in [bot.lower() for bot in bots] and self.config.get("delete_all_bot_messages", True):
                return True, "bot_all_messages", 90

            return False, None, 0

        except Exception as e:
            log(f"判断消息删除条件失败: {e}")
            return False, None, 0

    async def check_mentions_for_keywords(self, event, keywords):
        try:
            for entity in event.message.entities:
                if isinstance(entity, MessageEntityMentionName):
                    user_id = entity.user_id
                    try:
                        user = await self.client.get_entity(user_id)
                        user_name = user.username or user.first_name or ""
                        if any(keyword.lower() in user_name.lower() for keyword in keywords):
                            return True
                    except Exception:
                        continue
        except Exception as e:
            log(f"检查提及失败: {e}")
        return False

    async def handle_system_message_once(self):
        """定时清理系统消息（例如入群/退群通知）"""
        log("开始定时清理系统消息...")
        async for dialog in self.client.iter_dialogs(limit=100):
            current_time = self.get_beijing_time()
            try:
                if dialog.is_group:
                    entity = dialog.entity
                    admins = await self.client.get_participants(dialog, filter=ChannelParticipantsAdmins)
                    user = await self.client.get_me()
                    user_id = user.id
                    username = user.username if user.username is not None else 'None'
                    first_name = user.first_name if user.first_name is not None else 'None'
                    last_name = user.last_name if user.last_name is not None else 'None'

                    is_admin = any(admin.id == user_id for admin in admins)

                    if is_admin:
                        async for message in self.client.iter_messages(entity):
                            if message.action:
                                log(f"{current_time} 删除系统消息: {entity.title}  - {message.action}")
                                await self.client.delete_messages(entity, message.id)
                    else:
                        log(f"{current_time} {entity.title} 用户{username}-{first_name}-{last_name}不是管理员，跳过")
                else:
                    pass

            except RPCError as rpc_error:
                if rpc_error.code == 400 and "CHANNEL_MONOFORUM_UNSUPPORTED" in str(rpc_error):
                    log(f"{current_time} {entity.title} 该群不支持单一论坛。")
                else:
                    log(f"{current_time} {entity.title} 处理RPC错误: {rpc_error}")
            except ValueError as e:
                log(f"发生了错误ValueError: {e}")
                await asyncio.sleep(6)
            except Exception as e:
                log(f"{current_time} 系统消息清理失败: {e}")
                await asyncio.sleep(6)
                traceback.print_exc()
        log("系统消息清理完成")

    async def periodic_system_cleanup(self):
        """后台任务：每隔 30 分钟执行一次系统消息清理"""
        while True:
            await asyncio.sleep(1800)   # 30 分钟
            try:
                await self.handle_system_message_once()
            except Exception as e:
                log(f"定时系统清理出错: {e}")

    async def handle_bot_message(self, event):
        """处理机器人消息（带并发控制）"""
        async with self.semaphore:      # 限制同时处理的消息数量
            await asyncio.sleep(1)
            try:
                current_time = self.get_beijing_time()
                if event.out:
                    return

                result = await self.should_delete_message(event)

                if isinstance(result, tuple) and len(result) == 3:
                    should_delete, reason, delay_seconds = result
                else:
                    should_delete = False
                    reason = "unknown"
                    delay_seconds = 0
                    log(f"⚠️ should_delete_message 返回了意外的格式: {result}")

                if should_delete:
                    sender = await event.get_sender()
                    sender_name = sender.username if sender and sender.username else "Unknown"
                    message_preview = event.message.text[:50] + "..." if event.message.text and len(event.message.text) > 50 else event.message.text
                    event_time = self.get_beijing_time(event.date)
                    log(f"?? 检测到需删除的消息 | 原因: {reason} | 延迟: {delay_seconds}秒 | 发送时间: {event_time} ")
                    log(f"   发送者: @{sender_name}")
                    log(f"   消息预览: {message_preview}")

                    await self.delete_message_with_delay(event, delay_seconds)
                else:
                    if self.config.get("debug_mode", False):
                        sender = await event.get_sender()
                        if sender and sender.username:
                            event_time = self.get_beijing_time(event.date)
                            log(f"?? 收到消息 | 发送者: @{sender.username} | 发送时间:{event_time} | 无需删除")

            except Exception as e:
                current_time = self.get_beijing_time()
                log(f"处理消息时发生错误: {e} | 时间: {current_time} (北京时间)")

    async def delete_message_with_delay(self, event, delay_seconds=2):
        try:
            if delay_seconds > 10:
                log(f"⏰ 将在 {delay_seconds} 秒后删除消息...")

            await asyncio.sleep(delay_seconds)

            sender = await event.get_sender()
            chat_id = event.chat_id
            message_id = event.id

            try:
                message = await self.client.get_messages(chat_id, ids=message_id)
                if message:
                    await self.client.delete_messages(chat_id, message_id)

                    event_time = self.get_beijing_time(event.date)
                    sender_name = sender.username if sender and sender.username else "Unknown"
                    nowtime = self.get_beijing_time()
                    log(f"✅ 已删除消息 | 发送者: @{sender_name} | 延迟: {delay_seconds}秒 | 发送时间: {event_time} | 删除时间 {nowtime}")

                    if self.config.get("send_delete_notification", True):
                        await self.send_delete_notification(event, sender_name, event_time, delay_seconds)

                    return True
                else:
                    log("⚠️ 消息已不存在，跳过删除")
                    return False

            except errors.MessageDeleteForbiddenError:
                log("❌ 没有权限删除此消息")
                return False
            except errors.MessageIdInvalidError:
                log("⚠️ 消息ID无效，可能已被删除")
                return False

        except Exception as e:
            log(f"❌ 删除消息失败: {e}")
            return False

    async def send_delete_notification(self, event, sender_name, event_time, delay_seconds):
        try:
            reason_desc = "包含违规关键词" if delay_seconds <= 2 else "来自被监控的机器人"
            notification_text = (
                f"@{sender_name} 机器人的消息已被删除！\n"
                f"北京时间: {event_time} \n"
            )
            await self.client.send_message(event.chat_id, notification_text)
        except Exception as e:
            log(f"发送删除通知失败: {e}")

    async def start_monitoring(self):
        try:
            if not await self.initialize_client():
                return False

            # 只注册轻量的机器人消息处理器
            self.client.add_event_handler(
                self.handle_bot_message,
                events.NewMessage(incoming=True)
            )

            # 启动后台定时系统消息清理任务
            asyncio.create_task(self.periodic_system_cleanup())

            current_time = self.get_beijing_time()
            log("=" * 60)
            log("Telegram 机器人监控已启动")
            log(f"启动时间: {current_time} (北京时间)")
            log(f"不受监控的机器人: {self.get_bots_list()}")
            log(f"监控的关键词: {self.get_keywords_list()}")
            log(f"删除所有机器人消息: {self.config.get('delete_all_bot_messages', True)}")
            log("=" * 60)

            await self.client.run_until_disconnected()
            return True

        except Exception as e:
            current_time = self.get_beijing_time()
            log(f"监控过程中发生错误: {e} | 时间: {current_time} (北京时间)")
            return False
        finally:
            if self.client:
                await self.client.disconnect()

    async def cleanup(self):
        if self.client:
            await self.client.disconnect()


# ========== HTTP 状态服务 ==========
class StatusHandler(http.server.BaseHTTPRequestHandler):
    # 通过类属性传递密码，在启动服务器前设置
    web_passwd = None

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == '/status':
            qs = parse_qs(parsed_path.query)
            pass_input = qs.get('pass', [None])[0]

            if self.web_passwd and pass_input != self.web_passwd:
                self.send_response(200)
                self.send_header('Content-type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write("密码错误，拒绝访问\n".encode('utf-8'))
                return

            self.send_response(200)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            with log_lock:
                logs = list(log_buffer)
            if not logs:
                self.wfile.write("暂无日志\n".encode('utf-8'))
            else:
                self.wfile.write('\n'.join(logs).encode('utf-8'))
                self.wfile.write('\n'.encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not Found')

    def log_message(self, format, *args):
        # 抑制 HTTP 服务器自身的访问日志，避免干扰主日志
        pass


def start_http_server(web_passwd):
    StatusHandler.web_passwd = web_passwd
    port = int(os.environ.get('PORT', 10000))
    server = http.server.HTTPServer(('0.0.0.0', port), StatusHandler)
    log(f"HTTP 状态服务已启动，监听 0.0.0.0:{port}，访问 /status?pass=你的密码")
    server.serve_forever()


async def main():
    absolute_path = os.path.abspath(__file__)
    directory_path = os.path.dirname(absolute_path)

    json_config_file = os.path.join(directory_path, "delebot.json")

    if os.path.exists(json_config_file):
        config_file = json_config_file
        log(f"使用配置文件: {config_file}")
    else:
        log(f"配置文件不存在: {json_config_file}")
        return

    # 提前读取密码
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        web_passwd = config.get('web_passwd', '')
    except:
        web_passwd = ''

    # 启动 HTTP 状态服务线程
    http_thread = threading.Thread(target=start_http_server, args=(web_passwd,), daemon=True)
    http_thread.start()

    monitor = TelegramBotMonitor(config_file)

    try:
        await monitor.start_monitoring()
    except KeyboardInterrupt:
        current_time = monitor.get_beijing_time()
        log(f"\n👋 监控程序被用户中断 | 时间: {current_time} (北京时间)")
    except Exception as e:
        current_time = monitor.get_beijing_time()
        log(f"❌ 程序运行异常: {e} | 时间: {current_time} (北京时间)")
    finally:
        await monitor.cleanup()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(main())
