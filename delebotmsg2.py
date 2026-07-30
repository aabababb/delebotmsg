from telethon.tl.types import InputPeerUser, InputPeerChannel, MessageEntityMentionName, MessageService,PeerUser,Channel,ChannelParticipantsAdmins
from telethon import TelegramClient, sync, events, errors
import sys, asyncio
import queue, time, json, os, re,traceback
from datetime import datetime, timezone, timedelta
from telethon.errors import RPCError



class TelegramBotMonitor:
    def __init__(self, config_file):
        self.config_file = config_file
        self.client = None
        self.config = self.load_config()
        
    def load_config(self):
        """安全地加载配置文件"""
        if not os.path.exists(self.config_file):
            print(f"配置文件不存在: {self.config_file}")
            return {"bots": [], "keywords": []}
        
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                # 支持JSON格式配置
                if self.config_file.endswith('.json'):
                    return json.load(f)

        except Exception as e:
            print(f"加载配置文件失败: {e}")
            return {"bots": [], "keywords": []}
    
    def get_beijing_time(self, dt=None):
        """获取北京时间"""
        if dt is None:
            dt = datetime.now(timezone.utc)
        
        # 如果dt是naive（没有时区信息），假设它是UTC时间
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        # 转换为北京时间 (UTC+8)
        beijing_tz = timezone(timedelta(hours=8))
        beijing_time = dt.astimezone(beijing_tz)
        return beijing_time.strftime('%Y-%m-%d %p %H:%M:%S')
    
    def get_bots_list(self):
        """获取机器人列表"""
        bots = []
        if "bots" in self.config:
            return self.config["bots"]
        
        # 兼容旧格式
        for key, value in self.config.items():
            if "bot" in key.lower():
                bots.append(value)
        return bots
    
    def get_keywords_list(self):
        """获取关键词列表"""
        keywords = []
        if "keywords" in self.config:
            return self.config["keywords"]
        
        # 兼容旧格式
        for key, value in self.config.items():
            if "str" in key.lower() or "keyword" in key.lower():
                keywords.append(value)
        return keywords
    
    async def initialize_client(self):
        """初始化Telegram客户端"""
        try:
            api_id = self.config.get("api_id") 
            api_hash = self.config.get("api_hash")
            phone = self.config.get("phone") 
            
            self.client = TelegramClient(
                'delebot_session', 
                api_id, 
                api_hash
            )
            
            await self.client.connect()
            
            if not await self.client.is_user_authorized():
                await self.handle_authentication(phone)
                
            print("客户端初始化成功，开始监听消息...")
            return True
            
        except Exception as e:
            print(f"初始化客户端失败: {e}")
            return False
    
    async def handle_authentication(self, phone):
        """处理用户认证"""
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
        """判断是否应该删除消息"""
        try:
            sender = await event.get_sender()
            if not sender or not hasattr(sender, 'username') or not sender.username:
                return False, None, 0  # 返回三个值：是否删除、原因、延迟时间
            
            message_text = event.message.text or event.message.raw_text or ""
            sender_username = sender.username.lower()
            
            bots = self.get_bots_list()
            keywords = self.get_keywords_list()
            
            # 检查是否是目标机器人
            is_target_bot = any(bot.lower() == sender_username for bot in bots)
            
            # 检查消息内容是否包含关键词
            has_keyword = any(keyword.lower() in message_text.lower() for keyword in keywords)
            
            # 检查提及中的用户名是否包含关键词
            if not has_keyword and event.message.entities:
                has_keyword = await self.check_mentions_for_keywords(event, keywords)

            
            # 删除条件：
            # 1. 机器人且包含关键词 → 2秒延迟删除
            if "bot" in sender_username and has_keyword:
                return True, "bot_with_keyword", 3
            # 2. 机器人但没有关键词 → 60秒延迟删除
            elif "bot" in sender_username and sender_username not in [bot.lower() for bot in bots] and self.config.get("delete_all_bot_messages", True):
                return True, "bot_all_messages", 90
                
            return False, None, 0
            
        except Exception as e:
            print(f"判断消息删除条件失败: {e}")
            return False, None, 0

    async def check_mentions_for_keywords(self, event, keywords):
        """检查提及中是否包含关键词"""
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
        print("handle_system_message...")
        async for dialog in self.client.iter_dialogs(limit=100):
            current_time = self.get_beijing_time()
            try:
                if dialog.is_group:
                    entity = dialog.entity
                    # 获取群组的管理员列表
                    admins = await self.client.get_participants(dialog, filter=ChannelParticipantsAdmins)
                    user = await self.client.get_me()
                    user_id = user.id
                    username = user.username if user.username is not None else 'None'
                    first_name = user.first_name if user.first_name is not None else 'None'
                    last_name = user.last_name if user.last_name is not None else 'None'

                    is_admin = any(admin.id == user_id for admin in admins)

                    if is_admin:
                        #print(f"{current_time} 正在检查群组: {entity.title} (ID: {entity.id})， 用户是管理员")
                        
                        # 获取群组中的所有消息
                        async for message in self.client.iter_messages(entity):
                            # 检查消息类型是否为系统消息
                            if message.action: 
                                print(f"{current_time} 删除system消息: {entity.title}  - {message.action}")
                                await self.client.delete_messages(entity, message.id)
                    else:
                        for admin in admins:
                            print(f'Admin ID: {admin.id}, Admin Username: {admin.username}')
                        print(f"{current_time} {entity.title} 用户{username}-{first_name}-{last_name}不是管理员")
                else:
                    #print(f"{current_time} 跳过不支持的对话类型: {dialog.title} (ID: {dialog.id})")
                    pass

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
        #print("Handling bot message...")
        await asyncio.sleep(1)
        try:
            current_time = self.get_beijing_time()

            # 忽略自己的消息
            if event.out:
                return
            
            # 安全地获取删除判断结果
            result = await self.should_delete_message(event)
            
            # 处理返回值，确保是元组
            if isinstance(result, tuple) and len(result) == 3:
                should_delete, reason, delay_seconds = result
            else:
                # 如果返回的不是元组，默认不删除
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
                
                # 使用从判断函数返回的延迟时间
                await self.delete_message_with_delay(event, delay_seconds)
            else:
                # 可选：记录所有消息用于调试
                if self.config.get("debug_mode", False):
                    sender = await event.get_sender()
                    if sender and sender.username:
                        event_time = self.get_beijing_time(event.date)
                        print(f"?? 收到消息 | 发送者: @{sender.username} | 发送时间:{event_time} | 无需删除")
                        
        except Exception as e:
            current_time = self.get_beijing_time()
            print(f"处理消息时发生错误: {e} | 时间: {current_time} (北京时间)")

    async def delete_message_with_delay(self, event, delay_seconds=2):
        """延迟删除消息"""
        try:
            # 根据不同的延迟时间显示不同的提示
            if delay_seconds > 10:
                print(f"⏰ 将在 {delay_seconds} 秒后删除消息...")
            
            # 等待指定时间
            await asyncio.sleep(delay_seconds)
            
            sender = await event.get_sender()
            chat_id = event.chat_id
            message_id = event.id
            
            # 验证消息仍然存在
            try:
                message = await self.client.get_messages(chat_id, ids=message_id)
                if message:
                    # 删除消息
                    await self.client.delete_messages(chat_id, message_id)
                    
                    # 记录日志（使用北京时间）
                    event_time = self.get_beijing_time(event.date)
                    sender_name = sender.username if sender and sender.username else "Unknown"
                    nowtime = self.get_beijing_time() 
                    print(f"✅ 已删除消息 | 发送者: @{sender_name} | 延迟: {delay_seconds}秒 | 发送时间: {event_time} | 删除时间 {nowtime}")
                    
                    # 可选：发送删除通知
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
        """发送删除通知"""
        try:
            if delay_seconds <= 2:
                reason_desc = "包含违规关键词"
            else:
                reason_desc = "来自被监控的机器人"
                
            notification_text = (
                f"@{sender_name} 机器人的消息已被删除！\n"
                f"北京时间: {event_time} \n"
                #f"原因: {reason_desc}\n"
                #f"延迟: {delay_seconds}秒"
            )
            await self.client.send_message(event.chat_id, notification_text)
        except Exception as e:
            print(f"发送删除通知失败: {e}")




    async def combined_handler(self,event):
        try:
            # 使用 asyncio.gather 同时执行两个处理器
            await asyncio.gather(
                self.handle_system_message(event),
                self.handle_bot_message(event)
            )
        except Exception as e:
            print(f"Error in combined_handler: {e}")


    
    async def start_monitoring(self):
        """开始监控"""
        try:
            if not await self.initialize_client():
                return False
            
            # 注册消息处理器
            #print(f"当前解析模式: {self.client.parse_mode}")


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
            
            # 保持运行
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
    # 获取配置文件路径
    absolute_path = os.path.abspath(__file__)    
    directory_path = os.path.dirname(absolute_path)
    
    # 优先尝试JSON配置文件
    json_config_file = os.path.join(directory_path, "bot_config.json")
    
    if os.path.exists(json_config_file):
        config_file = json_config_file
        print(f"使用配置文件: {config_file}")

    else:
        print(f"配置文件不存在: {config_file}")
    
    # 创建监控实例
    monitor = TelegramBotMonitor(config_file)
    
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
    # 设置事件循环策略（Windows系统需要）
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # 运行主程序
    asyncio.run(main())
