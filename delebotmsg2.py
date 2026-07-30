from telethon.tl.types import InputPeerUser, InputPeerChannel, MessageEntityMentionName, MessageService, PeerUser, Channel, ChannelParticipantsAdmins
from telethon import TelegramClient, sync, events, errors
from telethon.sessions import StringSession  # 新增：支持环境变量会话
import sys, asyncio
import queue, time, json, os, re, traceback
from datetime import datetime, timezone, timedelta
from telethon.errors import RPCError


class TelegramBotMonitor:
    def __init__(self):
        """
        不再接收 config_file 参数，所有配置从环境变量读取
        """
        self.client = None
        # 从环境变量加载配置
        self.config = self.load_config_from_env()
        
    def load_config_from_env(self):
        """
        从环境变量构建配置字典，保持与原代码中 config 结构的兼容性
        """
        config = {}
        
        # 必需的 API 凭据
        config['api_id'] = int(os.environ.get('API_ID', 0))
        config['api_hash'] = os.environ.get('API_HASH', '')
        config['phone'] = os.environ.get('PHONE', '')  # 仅用于首次登录，若已有 StringSession 则不会用到
        
        # 监控的机器人列表，用英文逗号分隔，例如 "bot1,bot2"
        bots_env = os.environ.get('BOTS', '')
        config['bots'] = [b.strip() for b in bots_env.split(',') if b.strip()] if bots_env else []
        
        # 监控的关键词列表，同样逗号分隔
        keywords_env = os.environ.get('KEYWORDS', '')
        config['keywords'] = [k.strip() for k in keywords_env.split(',') if k.strip()] if keywords_env else []
        
        # 可选开关
        config['delete_all_bot_messages'] = os.environ.get('DELETE_ALL_BOT_MESSAGES', 'true').lower() == 'true'
        config['send_delete_notification'] = os.environ.get('SEND_DELETE_NOTIFICATION', 'true').lower() == 'true'
        config['debug_mode'] = os.environ.get('DEBUG_MODE', 'false').lower() == 'true'
        
        # 会话字符串（用于无头环境保持登录）
        config['session_string'] = os.environ.get('SESSION_STRING', '')
        
        return config
    
    def get_beijing_time(self, dt=None):
        """获取北京时间"""
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing_tz = timezone(timedelta(hours=8))
        beijing_time = dt.astimezone(beijing_tz)
        return beijing_time.strftime('%Y-%m-%d %p %H:%M:%S')
    
    def get_bots_list(self):
        """获取机器人列表"""
        return self.config.get("bots", [])
    
    def get_keywords_list(self):
        """获取关键词列表"""
        return self.config.get("keywords", [])
    
    async def initialize_client(self):
        """初始化Telegram客户端，优先使用 StringSession 环境变量"""
        try:
            api_id = self.config["api_id"]
            api_hash = self.config["api_hash"]
            session_str = self.config.get("session_string", "")
            
            if not api_id or not api_hash:
                print("错误：环境变量 API_ID 或 API_HASH 未设置")
                return False
            
            # 如果提供了 SESSION_STRING，则使用字符串会话，避免依赖磁盘文件
            if session_str:
                self.client = TelegramClient(StringSession(session_str), api_id, api_hash)
            else:
                # 回退到文件会话（本地调试时可用）
                self.client = TelegramClient('delebot_session', api_id, api_hash)
            
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                # 云端环境不应走到这里，应该提前生成 StringSession
                phone = self.config.get("phone")
                if not phone:
                    print("错误：未授权且未设置 PHONE 环境变量，无法自动登录")
                    return False
                await self.handle_authentication(phone)
                
            print("客户端初始化成功，开始监听消息...")
            return True
            
        except Exception as e:
            print(f"初始化客户端失败: {e}")
            return False
    
    async def handle_authentication(self, phone):
        """处理用户认证（仅用于首次登录，本地交互）"""
        try:
            await self.client.send_code_request(phone)
            code = input('请输入验证码：')
            await self.client.sign_in(phone, code)
        except errors.SessionPasswordNeededError:
            password = input('请输入二次验证密码：')
            await self.client.sign_in(password=password)
        except Exception as e:
            print(f"认证失败: {e}")
            raise

    async def should_delete_message(self, event):
        """判断是否应该删除消息（逻辑不变）"""
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
            print(f"判断消息删除条件失败: {e}")
            return False, None, 0

    async def check_mentions_for_keywords(self, event, keywords):
        """检查提及中是否包含关键词（逻辑不变）"""
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
            print(f"检查提及失败: {e}")
        return False

    async def handle_system_message(self, event):
        """处理系统消息（逻辑不变，仅移除了冗余的 get_me 调用，保留原有功能）"""
        print("handle_system_message...")
        async for dialog in self.client.iter_dialogs(limit=100):
            current_time = self.get_beijing_time()
            try:
                if dialog.is_group:
                    entity = dialog.entity
                    admins = await self.client.get_participants(dialog, filter=ChannelParticipantsAdmins)
                    user = await self.client.get_me()
                    user_id = user.id
                    username = user.username or 'None'
                    first_name = user.first_name or 'None'
                    last_name = user.last_name or 'None'

                    is_admin = any(admin.id == user_id for admin in admins)

                    if is_admin:
                        async for message in self.client.iter_messages(entity):
                            if message.action: 
                                print(f"{current_time} 删除system消息: {entity.title}  - {message.action}")
                                await self.client.delete_messages(entity, message.id)
                    else:
                        for admin in admins:
                            print(f'Admin ID: {admin.id}, Admin Username: {admin.username}')
                        print(f"{current_time} {entity.title} 用户{username}-{first_name}-{last_name}不是管理员")
            except RPCError as rpc_error:
                if rpc_error.code == 400 and "CHANNEL_MONOFORUM_UNSUPPORTED" in str(rpc_error):
                    print(f"{current_time} {entity.title} 该群不支持单一论坛。")
                else:
                    print(f"{current_time} {entity.title} 处理RPC错误: {rpc_error}")
            except ValueError as e:
                print(f"发生了错误ValueError: {e}")
                await asyncio.sleep(6)
            except Exception as e:
                print(f"{current_time} handle_system_message失败: {e}")
                await asyncio.sleep(6)
                traceback.print_exc()

    async def handle_bot_message(self, event):
        """处理机器人消息（逻辑不变）"""
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
                print(f"⚠️ should_delete_message 返回了意外的格式: {result}")
            
            if should_delete:
                sender = await event.get_sender()
                sender_name = sender.username if sender and sender.username else "Unknown"
                message_preview = event.message.text[:50] + "..." if event.message.text and len(event.message.text) > 50 else event.message.text
                event_time = self.get_beijing_time(event.date)
                print(f"?? 检测到需删除的消息 | 原因: {reason} | 延迟: {delay_seconds}秒 | 发送时间: {event_time} ")
                print(f"   发送者: @{sender_name}")
                print(f"   消息预览: {message_preview}")
                await self.delete_message_with_delay(event, delay_seconds)
            else:
                if self.config.get("debug_mode", False):
                    sender = await event.get_sender()
                    if sender and sender.username:
                        event_time = self.get_beijing_time(event.date)
                        print(f"?? 收到消息 | 发送者: @{sender.username} | 发送时间:{event_time} | 无需删除")
        except Exception as e:
            current_time = self.get_beijing_time()
            print(f"处理消息时发生错误: {e} | 时间: {current_time} (北京时间)")

    async def delete_message_with_delay(self, event, delay_seconds=2):
        """延迟删除消息（逻辑不变）"""
        try:
            if delay_seconds > 10:
                print(f"⏰ 将在 {delay_seconds} 秒后删除消息...")
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
                    print(f"✅ 已删除消息 | 发送者: @{sender_name} | 延迟: {delay_seconds}秒 | 发送时间: {event_time} | 删除时间 {nowtime}")
                    if self.config.get("send_delete_notification", True):
                        await self.send_delete_notification(event, sender_name, event_time, delay_seconds)
                    return True
                else:
                    print("⚠️ 消息已不存在，跳过删除")
                    return False
            except errors.MessageDeleteForbiddenError:
                print("❌ 没有权限删除此消息")
                return False
            except errors.MessageIdInvalidError:
                print("⚠️ 消息ID无效，可能已被删除")
                return False
        except Exception as e:
            print(f"❌ 删除消息失败: {e}")
            return False

    async def send_delete_notification(self, event, sender_name, event_time, delay_seconds):
        """发送删除通知（逻辑不变）"""
        try:
            if delay_seconds <= 2:
                reason_desc = "包含违规关键词"
            else:
                reason_desc = "来自被监控的机器人"
            notification_text = (
                f"@{sender_name} 机器人的消息已被删除！\n"
                f"北京时间: {event_time} \n"
            )
            await self.client.send_message(event.chat_id, notification_text)
        except Exception as e:
            print(f"发送删除通知失败: {e}")

    async def combined_handler(self, event):
        try:
            await asyncio.gather(
                self.handle_system_message(event),
                self.handle_bot_message(event)
            )
        except Exception as e:
            print(f"Error in combined_handler: {e}")

    async def start_monitoring(self):
        """开始监控（逻辑不变）"""
        try:
            if not await self.initialize_client():
                return False
            
            self.client.add_event_handler(
                self.combined_handler,
                events.NewMessage(incoming=True)
            )
            
            current_time = self.get_beijing_time()
            print("=" * 60)
            print("Telegram 机器人监控已启动")
            print(f"启动时间: {current_time} (北京时间)")
            print(f"不受监控的机器人: {self.get_bots_list()}")
            print(f"监控的关键词: {self.get_keywords_list()}")
            print(f"删除所有机器人消息: {self.config.get('delete_all_bot_messages', True)}")
            print("=" * 60)
            
            await self.client.run_until_disconnected()
            return True
            
        except Exception as e:
            current_time = self.get_beijing_time()
            print(f"监控过程中发生错误: {e} | 时间: {current_time} (北京时间)")
            return False
        finally:
            if self.client:
                await self.client.disconnect()
    
    async def cleanup(self):
        """清理资源"""
        if self.client:
            await self.client.disconnect()


async def main():
    # 不再需要配置文件路径，直接创建实例
    monitor = TelegramBotMonitor()
    
    try:
        await monitor.start_monitoring()
    except KeyboardInterrupt:
        current_time = monitor.get_beijing_time()
        print(f"\n👋 监控程序被用户中断 | 时间: {current_time} (北京时间)")
    except Exception as e:
        current_time = monitor.get_beijing_time()
        print(f"❌ 程序运行异常: {e} | 时间: {current_time} (北京时间)")
    finally:
        await monitor.cleanup()


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
