import asyncio
import os
import base64
from typing import List, Dict, Optional
import datetime
import time
import json
import zipfile
import pyzipper
import chardet
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import subprocess

from astrbot.api.event import filter, AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Reply, Plain, Node, File
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_file_checker",
    "Foolllll",
    "群文件失效检查",
    "1.3",
    "https://github.com/Foolllll-J/astrbot_plugin_file_checker"
)
class GroupFileCheckerPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        self.group_whitelist: List[int] = self.config.get("group_whitelist", [])
        self.group_whitelist = [int(gid) for gid in self.group_whitelist]
        self.notify_on_success: bool = self.config.get("notify_on_success", True)
        self.pre_check_delay_seconds: int = self.config.get("pre_check_delay_seconds", 10)
        self.check_delay_seconds: int = self.config.get("check_delay_seconds", 300)
        self.preview_length: int = self.config.get("preview_length", 200)
        self.forward_threshold: int = self.config.get("forward_threshold", 300)
        self.enable_zip_preview: bool = self.config.get("enable_zip_preview", True)
        self.default_zip_password: str = self.config.get("default_zip_password", "")
        self.enable_repack_on_failure: bool = self.config.get("enable_repack_on_failure", False)
        self.repack_zip_password: str = self.config.get("repack_zip_password", "")
        
        self.download_semaphore = asyncio.Semaphore(5)
        logger.info("插件 [群文件失效检查] 已加载。")

    def _find_file_component(self, event: AstrMessageEvent) -> Optional[Comp.File]:
        for segment in event.get_messages():
            if isinstance(segment, Comp.File):
                return segment
        return None

    def _fix_zip_filename(self, filename: str) -> str:
        try:
            return filename.encode('cp437').decode('gbk')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return filename

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=2)
    async def on_group_message(self, event: AstrMessageEvent, *args, **kwargs):
        group_id = int(event.get_group_id())
        if self.group_whitelist and group_id not in self.group_whitelist:
            return
        try:
            raw_event_data = event.message_obj.raw_message
            message_list = raw_event_data.get("message")
            if not isinstance(message_list, list):
                return
            for segment_dict in message_list:
                if isinstance(segment_dict, dict) and segment_dict.get("type") == "file":
                    data_dict = segment_dict.get("data", {})
                    file_name = data_dict.get("file")
                    file_id = data_dict.get("file_id")
                    if file_name and file_id:
                        logger.info(f"【原始方式】成功解析: 文件名='{file_name}', ID='{file_id}'")
                        file_component = self._find_file_component(event)
                        if not file_component:
                            logger.error("致命错误：无法在高级组件中找到对应的File对象！")
                            return
                        async for result in self._handle_file_check_flow(event, file_name, file_id, file_component):
                            yield result
                        break
        except Exception as e:
            logger.error(f"【原始方式】处理消息时发生致命错误: {e}", exc_info=True)

    async def _send_or_forward(self, event: AstrMessageEvent, text: str, message_id: int):
        if self.forward_threshold <= 0 or len(text) <= self.forward_threshold:
            chain = MessageChain([Reply(id=message_id), Plain(text=text)])
            await event.send(chain)
            return
        logger.info(f"[{event.get_group_id()}] 检测到长消息，将自动合并转发。")
        try:
            forward_node = Node(uin=event.get_self_id(), name="文件检查报告", content=[Reply(id=message_id), Plain(text=text)])
            await event.send(MessageChain([forward_node]))
        except Exception as e:
            logger.error(f"[{event.get_group_id()}] 合并转发长消息时出错: {e}", exc_info=True)
            fallback_text = text[:self.forward_threshold] + "... (消息过长且合并转发失败)"
            chain = MessageChain([Reply(id=message_id), Plain(text=fallback_text)])
            await event.send(chain)

    async def _repack_and_send_txt(self, event: AstrMessageEvent, original_filename: str, file_component: Comp.File):
        temp_dir = os.path.join(get_astrbot_data_path(), "plugins_data", "file_checker", "temp")
        os.makedirs(temp_dir, exist_ok=True)
        
        repacked_file_path = None
        original_txt_path = None
        renamed_txt_path = None
        original_cwd = os.getcwd()  # 保存当前工作目录
        try:
            logger.info(f"开始为失效文件 {original_filename} 进行重新打包...")
            
            original_txt_path = await file_component.get_file()
            
            # 1. 切换到临时目录
            os.chdir(temp_dir)
            
            # 2. 重命名临时文件，以便 zip 命令使用正确的文件名
            temp_file_base = os.path.basename(original_txt_path)
            renamed_txt_path = os.path.join(temp_dir, original_filename)
            if os.path.exists(renamed_txt_path):
                os.remove(renamed_txt_path)
            os.rename(original_txt_path, renamed_txt_path)
            
            # 3. 设置打包后的文件路径，这里需要使用绝对路径，因为我们改变了工作目录
            base_name = os.path.splitext(original_filename)[0]
            new_zip_name = f"{base_name}.zip"
            repacked_file_path = os.path.join(temp_dir, f"{int(time.time())}_{new_zip_name}")
            
            # 4. 构建命令
            # 关键：我们只传递文件名给 zip 命令，因为我们已经切换了工作目录
            command = ['zip', os.path.basename(repacked_file_path), os.path.basename(renamed_txt_path)]
            if self.repack_zip_password:
                command.extend(['-P', self.repack_zip_password])
            
            logger.info(f"正在执行打包命令: {' '.join(command)}")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                error_message = stderr.decode('utf-8')
                logger.error(f"使用 zip 命令打包文件时出错: {error_message}")
                yield event.plain_result(f"❌ 重新打包失败，错误信息：\n{error_message}")
                return
            
            logger.info(f"文件已重新打包至 {repacked_file_path}，准备发送...")
            
            reply_text = "已为您重新打包为ZIP文件发送："
            file_component_to_send = File(file=repacked_file_path, name=new_zip_name)
            
            yield event.plain_result(reply_text)
            yield event.chain_result([file_component_to_send])
                
        except FileNotFoundError:
            logger.error("重新打包失败：容器内未找到 zip 命令。请安装 zip。")
            yield event.plain_result("❌ 重新打包失败。容器内未找到 zip 命令，请联系管理员安装。")
        except Exception as e:
            logger.error(f"重新打包并发送文件时出错: {e}", exc_info=True)
            yield event.plain_result("❌ 重新打包并发送文件失败。")
        finally:
            # 恢复工作目录
            os.chdir(original_cwd)
            
            # 清理临时文件
            if repacked_file_path and os.path.exists(repacked_file_path):
                async def cleanup_file(path: str):
                    await asyncio.sleep(10)
                    try:
                        os.remove(path)
                        logger.info(f"已清理临时文件: {path}")
                    except OSError as e:
                        logger.warning(f"删除临时文件 {path} 失败: {e}")
                asyncio.create_task(cleanup_file(repacked_file_path))

            if renamed_txt_path and os.path.exists(renamed_txt_path):
                try:
                    os.remove(renamed_txt_path)
                    logger.info(f"已清理原始临时文件: {renamed_txt_path}")
                except OSError as e:
                    logger.warning(f"删除原始临时文件 {renamed_txt_path} 失败: {e}")

# ... (其他代码保持不变)
