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
        bots = []
        if "bots" in self.config:
            bots = self.config["bots"]
        else:
            for key, value in self.config.items():
                if "bot" in key.lower():
                    bots.append(value)
        # 清理@符号和空格，统一小写
        cleaned = []
        for b in bots:
            b = str(b).strip().lstrip('@').lower()
            if b:
                cleaned.append(b)
        return cleaned

    def get_keywords_list(self):
        if "keywords" in self.config:
            return self.config["keywords"]
        keywords = []
        for key, value in self.config.items():
            if "str" in key.lower() or "keyword" in key.lower():
                keywords.append(value)
        return keywords

    async def initialize_client(self):
        try:
            api_id = self.config.get("api_id")
            api_hash = self.config.get("api_hash")
            string_session = self.config.get("string_session")
            if not string_session:
                raise Exception("配置文件中缺少 string_session 字段")
            self.client = TelegramClient(StringSession(string_session), api_id, api_hash)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise Exception("StringSession 无效或已过期")
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
            has_keyword = any(keyword.lower() in message_text.lower() for keyword in keywords)
            if not has_keyword and event.message.entities:
                has_keyword = await self.check_mentions_for_keywords(event, keywords)

            if "bot" in sender_username and has_keyword:
                return True, "bot_with_keyword", 3
            elif "bot" in sender_username and sender_username not in bots and self.config.get("delete_all_bot_messages", True):
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

    # ============ 新增辅助方法：检查用户是否是群组管理员 ============
    async def is_user_admin(self, chat, user_id):
        """判断指定用户是否为群组管理员"""
        try:
            admins = await self.client.get_participants(chat, filter=ChannelParticipantsAdmins)
            return any(admin.id == user_id for admin in admins)
        except Exception as e:
            log(f"获取管理员列表失败: {e}")
            return False

    # ============ 新增辅助方法：封禁机器人 ============

    async def ban_bot(self, chat, user):
        try:
            try:
                input_entity = await self.client.get_input_entity(user.id)
            except:
                username = getattr(user, 'username', None)
                if username:
                    input_entity = await self.client.get_input_entity(username)
                else:
                    raise
            await self.client.edit_permissions(chat, input_entity, view_messages=False)
            return True
        except errors.ChatAdminRequiredError:
            log("❌ 本账号不是管理员或缺少封禁权限，无法 ban 机器人")
            return False
        except Exception as e:
            log(f"❌ 封禁机器人失败: {e}")
            return False

    async def handle_system_message_once(self):
        log("开始定时清理系统消息...")
        async for dialog in self.client.iter_dialogs(limit=100):
            current_time = self.get_beijing_time()
            try:
                if dialog.is_group:
                    entity = dialog.entity
                    admins = await self.client.get_participants(dialog, filter=ChannelParticipantsAdmins)
                    user = await self.client.get_me()
                    user_id = user.id
                    is_admin = any(admin.id == user_id for admin in admins)
                    if is_admin:
                        async for message in self.client.iter_messages(entity):
                            if message.action:
                                log(f"{current_time} 删除系统消息: {entity.title} - {message.action}")
                                await self.client.delete_messages(entity, message.id)
                    else:
                        log(f"{current_time} {entity.title} 不是管理员，跳过清理")
            except RPCError as e:
                log(f"{current_time} RPC错误: {e}")
            except Exception as e:
                log(f"{current_time} 系统消息清理失败: {e}")
        log("系统消息清理完成")

    async def periodic_system_cleanup(self):
        while True:
            await asyncio.sleep(1800)
            try:
                await self.handle_system_message_once()
            except Exception as e:
                log(f"定时清理出错: {e}")

    async def handle_new_member(self, event):
        """当有新成员加入群组时，如果是机器人且邀请者不是管理员，则踢出"""
        if not event.is_group or not event.added_by or not event.users:
            return
        if not (event.user_added or event.user_joined):
            return

        chat = await event.get_chat()
        try:
            inviter = await self.client.get_entity(event.added_by)
        except Exception as e:
            log(f"无法获取邀请者信息: {e}")
            return

        for user in event.users:
            if not user.bot:
                continue
            try:
                admins = await self.client.get_participants(chat, filter=ChannelParticipantsAdmins)
                admin_ids = [admin.id for admin in admins] if admins else []
                is_inviter_admin = inviter.id in admin_ids
            except Exception as e:
                log(f"获取管理员列表失败: {e}")
                continue

            if not is_inviter_admin:
                try:
                    await self.client.kick_participant(chat, user)
                    log(f"✅ 已踢出非管理员邀请的机器人: @{user.username or user.id} (邀请者: @{inviter.username or inviter.id})")
                except Exception as e:
                    log(f"❌ 踢出机器人失败: {e}")

    async def handle_bot_message(self, event):
        async with self.semaphore:
            await asyncio.sleep(1)
            try:
                if event.out:
                    return

                # ============ 新增功能：处理群组内非管理员机器人消息 ============
                if event.is_group and self.config.get("ban_non_admin_bots", True):
                    sender = await event.get_sender()
                    if sender and getattr(sender, 'bot', False):
                        # 检查该机器人是否是管理员
                        if not await self.is_user_admin(event.chat_id, sender.id):
                            log(f"🚨 检测到非管理员机器人发消息，立即删除并封禁: @{sender.username or sender.id}")
                            # 删除消息
                            try:
                                await self.client.delete_messages(event.chat_id, event.message.id)
                                log("✅ 消息已删除")
                            except Exception as e:
                                log(f"❌ 删除消息失败: {e}")
                            # 封禁机器人
                            if await self.ban_bot(event.chat_id, sender):
                                log(f"✅ 已封禁机器人 @{sender.username or sender.id}")
                            return  # 不再走原有删除逻辑

                # ============ 原有删除逻辑 ============
                result = await self.should_delete_message(event)
                if isinstance(result, tuple) and len(result) == 3:
                    should_delete, reason, delay_seconds = result
                else:
                    should_delete = False
                    reason = "unknown"
                    delay_seconds = 0
                if should_delete:
                    sender = await event.get_sender()
                    sender_name = sender.username if sender and sender.username else "Unknown"
                    message_preview = event.message.text[:50] + "..." if event.message.text and len(event.message.text) > 50 else event.message.text
                    event_time = self.get_beijing_time(event.date)
                    log(f"检测到需删除的消息 | 原因: {reason} | 延迟: {delay_seconds}秒 | 发送时间: {event_time}")
                    log(f"   发送者: @{sender_name} | 预览: {message_preview}")
                    await self.delete_message_with_delay(event, delay_seconds)
                elif self.config.get("debug_mode", False):
                    sender = await event.get_sender()
                    if sender and sender.username:
                        event_time = self.get_beijing_time(event.date)
                        log(f"收到消息 | 发送者: @{sender.username} | 时间:{event_time} | 无需删除")
            except Exception as e:
                log(f"处理消息时发生错误: {e}")

    async def delete_message_with_delay(self, event, delay_seconds=2):
        try:
            if delay_seconds > 10:
                log(f"将在 {delay_seconds} 秒后删除消息...")
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
                    log("消息已不存在，跳过删除")
                    return False
            except errors.MessageDeleteForbiddenError:
                log("没有权限删除此消息")
                return False
            except errors.MessageIdInvalidError:
                log("消息ID无效，可能已被删除")
                return False
        except Exception as e:
            log(f"删除消息失败: {e}")
            return False

    async def send_delete_notification(self, event, sender_name, event_time, delay_seconds):
        try:
            notification_text = f"@{sender_name} 机器人的消息已被删除！\n北京时间: {event_time}\n"
            await self.client.send_message(event.chat_id, notification_text)
        except Exception as e:
            log(f"发送删除通知失败: {e}")

    async def start_monitoring(self):
        try:
            if not await self.initialize_client():
                return False

            self.client.add_event_handler(
                self.handle_bot_message,
                events.NewMessage(incoming=True)
            )

            if self.config.get("kick_unauthorized_bots", True):
                self.client.add_event_handler(
                    self.handle_new_member,
                    events.ChatAction(func=lambda e: e.user_added or e.user_joined)
                )
                log("已启用「踢除非管理员邀请的机器人」功能")

            if self.config.get("ban_non_admin_bots", True):
                log("已启用「群组内非管理员机器人消息删除并封禁」功能")

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
            log(f"监控过程中发生错误: {e}")
            return False
        finally:
            await self.cleanup()

    async def cleanup(self):
        if self.client:
            await self.client.disconnect()


# ========== HTTP 状态服务 ==========
class StatusHandler(http.server.BaseHTTPRequestHandler):
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
        pass


def start_http_server(web_passwd):
    StatusHandler.web_passwd = web_passwd
    port = int(os.environ.get('PORT', 20000))
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

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        web_passwd = config.get('web_passwd', '')
    except:
        web_passwd = ''

    http_thread = threading.Thread(target=start_http_server, args=(web_passwd,), daemon=True)
    http_thread.start()

    monitor = TelegramBotMonitor(config_file)
    try:
        await monitor.start_monitoring()
    except KeyboardInterrupt:
        log(f"\n👋 监控程序被用户中断 | 时间: {monitor.get_beijing_time()}")
    except Exception as e:
        log(f"❌ 程序运行异常: {e} | 时间: {monitor.get_beijing_time()}")
    finally:
        await monitor.cleanup()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
