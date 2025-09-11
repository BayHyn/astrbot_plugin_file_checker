import asyncio
import os
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.0",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        
        self.notify_on_success: bool = self.config.get("notify_on_success", True)
        self.check_delay_seconds: int = self.config.get("check_delay_seconds", 30)
        
        self.download_semaphore = asyncio.Semaphore(5)
        
        logger.info("插件 [群文件失效检查] 已加载。")
        logger.info(f"成功时通知: {'开启' if self.notify_on_success else '关闭'}")
        logger.info(f"检查延时: {self.check_delay_seconds} 秒")
        
        if self.group_whitelist:
            logger.info(f"已启用群聊白名单，只在以下群组生效: {self.group_whitelist}")
        else:
            logger.info("未配置群聊白名单，插件将在所有群组生效。")

    # --- 新增: 文件大小格式化工具函数 ---
    def _format_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024**2:
            return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024**3:
            return f"{size_bytes/1024**2:.2f} MB"
        else:
            return f"{size_bytes/1024**3:.2f} GB"

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                file_name = segment.name
                logger.info(f"检测到群 {group_id} 中的文件消息: '{file_name}'，已加入处理队列。")
                asyncio.create_task(self._handle_file_check(event, file_name, segment))
                break

    async def _handle_file_check(self, event: AstrMessageEvent, file_name: str, file_component: Comp.File):
        await asyncio.sleep(self.check_delay_seconds)
        
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        async with self.download_semaphore:
            logger.info(f"[{group_id}] 开始检查文件: '{file_name or '未知文件名'}' (获得处理许可)")
            local_file_path = None
            try:
                local_file_path = await file_component.get_file()
                
                file_size_bytes = os.path.getsize(local_file_path)
                display_filename = os.path.basename(local_file_path)

                # --- 修改点: 使用新工具函数格式化文件大小 ---
                logger.info(f"[{group_id}] >> 文件已下载到: {local_file_path}")
                logger.info(f"[{group_id}] >> 临时文件名: {display_filename}, 文件大小: {self._format_size(file_size_bytes)}")

                MINIMUM_VALID_SIZE_BYTES = 1024
                if file_size_bytes < MINIMUM_VALID_SIZE_BYTES:
                    raise ValueError(f"文件大小 ({self._format_size(file_size_bytes)}) 过小，判定为失效文件。")

                is_text_file = False
                decoded_text = ""
                head_content_bytes = b''
                try:
                    with open(local_file_path, 'rb') as f:
                        head_content_bytes = f.read(1024)
                    
                    try:
                        decoded_text = head_content_bytes.decode('utf-8').strip()
                        is_text_file = True
                        logger.info(f"[{group_id}] >> 文件内容可被 UTF-8 解码，识别为文本文件。")
                    except UnicodeDecodeError:
                        try:
                            decoded_text = head_content_bytes.decode('gbk').strip()
                            is_text_file = True
                            logger.info(f"[{group_id}] >> 文件内容可被 GBK 解码，识别为文本文件。")
                        except UnicodeDecodeError:
                            logger.info(f"[{group_id}] >> 文件内容无法被常见编码解码，识别为二进制文件。")
                    
                    if is_text_file:
                        logger.info(f"[{group_id}] >> 文件头部内容 (解码后): {decoded_text[:200]}...") # 日志中也只打一部分
                
                except Exception as read_e:
                    logger.error(f"[{group_id}] >> 读取文件内容时失败: {read_e}")
                
                if not file_name:
                    file_name = display_filename
                
                logger.info(f"✅ [{group_id}] 文件 '{file_name}' 检查有效。")
                
                if self.notify_on_success:
                    try:
                        # --- 修改点: 构建新的、带预览的回复消息 ---
                        success_message = "✅ 您发送的文件检查有效，可以正常下载。"
                        if is_text_file and decoded_text:
                            # 截取前200个字符作为预览
                            preview_text = decoded_text[:200]
                            success_message += f"\n格式为TXT，以下是预览：\n{preview_text}"
                            # 如果预览内容被截断，则在末尾加上省略号
                            if len(decoded_text) > 200:
                                success_message += "..."
                        
                        chain = MessageChain([Reply(id=message_id), Plain(text=success_message)])
                        await event.send(chain)
                        logger.info(f"已向群 {group_id} 回复文件有效通知。")
                    except Exception as reply_e:
                        logger.error(f"向群 {group_id} 回复有效通知时发生错误: {reply_e}")
                
            except Exception as e:
                display_name_for_error = file_name or (display_filename if 'display_filename' in locals() else "未知文件")
                logger.error(f"❌ [{group_id}] 文件 '{display_name_for_error}' 经检查已失效! 原因: {e}")
                try:
                    failure_message = f"❌ 您发送的文件「{display_name_for_error}」经检查已失效或被服务器屏蔽。"
                    chain = MessageChain([Reply(id=message_id), Plain(text=failure_message)])
                    await event.send(chain)
                    logger.info(f"已向群 {group_id} 回复文件失效通知。")
                except Exception as send_e:
                    logger.error(f"向群 {group_id} 回复失效通知时再次发生错误: {send_e}")
            
            finally:
                if local_file_path and os.path.exists(local_file_path):
                    try:
                        os.remove(local_file_path)
                        logger.info(f"[{group_id}] 临时文件 '{local_file_path}' 已被删除。")
                    except OSError as e:
                        logger.warning(f"[{group_id}] 删除临时文件失败: {e}")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")