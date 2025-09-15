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
    
    # === 修复后的函数：通过文件名和时间戳搜索文件ID ===
    async def _search_file_id_by_name(self, event: AstrMessageEvent, file_name: str, search_time: float) -> Optional[str]:
        group_id = int(event.get_group_id())
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            file_list = await client.api.call_action('get_group_root_files', group_id=group_id)
            
            if not isinstance(file_list, dict) or 'files' not in file_list:
                logger.warning("get_group_root_files API调用返回了意料之外的格式。")
                return None
            
            # 遍历API返回的文件列表
            for file_info in file_list.get('files', []):
                # 检查文件名和上传时间是否匹配
                if file_info.get('file_name') == file_name and abs(file_info.get('upload_time', 0) - search_time) < 60:
                    logger.info(f"成功通过文件名搜索到文件ID: {file_info.get('file_id')}")
                    return file_info.get('file_id')
            return None
        except Exception as e:
            logger.error(f"通过文件名搜索文件ID时出错: {e}", exc_info=True)
            return None
    # ================================================================

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
        try:
            logger.info(f"开始为失效文件 {original_filename} 进行重新打包...")
            
            original_txt_path = await file_component.get_file()
            
            renamed_txt_path = os.path.join(temp_dir, original_filename)
            if os.path.exists(renamed_txt_path):
                os.remove(renamed_txt_path)
            os.rename(original_txt_path, renamed_txt_path)

            base_name = os.path.splitext(original_filename)[0]
            new_zip_name = f"{base_name}.zip"
            repacked_file_path = os.path.join(temp_dir, f"{int(time.time())}_{new_zip_name}")

            command = ['zip', '-j', repacked_file_path, renamed_txt_path]
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
            
            send_time = time.time()
            await event.send(MessageChain([file_component_to_send]))
            
            # 等待一小段时间，确保文件上传完成并出现在群文件列表
            await asyncio.sleep(2)
            
            new_file_id = None
            
            if not new_file_id:
                logger.warning("正在尝试通过文件名搜索新文件的ID。")
                new_file_id = await self._search_file_id_by_name(event, new_zip_name, send_time)
            
            if new_file_id:
                logger.info(f"新文件发送成功，ID为 {new_file_id}，已加入延时复核队列。")
                asyncio.create_task(self._task_delayed_recheck(event, new_zip_name, new_file_id, file_component, None))
            else:
                logger.error("未能获取新文件的ID，无法进行延时复核。")
            
        except FileNotFoundError:
            logger.error("重新打包失败：容器内未找到 zip 命令。请安装 zip。")
            yield event.plain_result("❌ 重新打包失败。容器内未找到 zip 命令，请联系管理员安装。")
        except Exception as e:
            logger.error(f"重新打包并发送文件时出错: {e}", exc_info=True)
            yield event.plain_result("❌ 重新打包并发送文件失败。")
        finally:
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
                    logger.info(f"已清理重命名后的临时文件: {renamed_txt_path}")
                except OSError as e:
                    logger.warning(f"删除临时文件 {renamed_txt_path} 失败: {e}")
    
    async def _handle_file_check_flow(self, event: AstrMessageEvent, file_name: str, file_id: str, file_component: Comp.File):
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()
        if sender_id == self_id:
            logger.info(f"[{group_id}] 机器人发送的文件，直接跳过处理。")
            return
            
        await asyncio.sleep(self.pre_check_delay_seconds)
        logger.info(f"[{group_id}] [阶段一] 开始即时检查: '{file_name}'")

        is_gfs_valid = await self._check_validity_via_gfs(event, file_id)

        preview_text, preview_extra_info = await self._get_preview_for_file(file_name, file_component)

        if is_gfs_valid:
            if self.notify_on_success:
                success_message = f"✅ 您发送的文件「{file_name}」初步检查有效。"
                if preview_text:
                    preview_text_short = preview_text[:self.preview_length]
                    success_message += f"\n{preview_extra_info}，以下是预览：\n{preview_text_short}"
                    if len(preview_text) > self.preview_length: success_message += "..."
                await self._send_or_forward(event, success_message, message_id)
            logger.info(f"[{group_id}] 初步检查通过，已加入延时复核队列。")
            asyncio.create_task(self._task_delayed_recheck(event, file_name, file_id, file_component, preview_text))
        else:
            logger.error(f"❌ [{group_id}] [阶段一] 文件 '{file_name}' 即时检查已失效!")
            try:
                failure_message = f"⚠️ 您发送的文件「{file_name}」已失效。"
                if preview_text:
                    preview_text_short = preview_text[:self.preview_length]
                    failure_message += f"\n{preview_extra_info}，以下是预览：\n{preview_text_short}"
                    if len(preview_text) > self.preview_length: failure_message += "..."
                await self._send_or_forward(event, failure_message, message_id)

                is_txt = file_name.lower().endswith('.txt')
                if self.enable_repack_on_failure and is_txt and preview_text:
                    logger.info("文件即时检查失效但内容可读，触发重新打包任务...")
                    async for result in self._repack_and_send_txt(event, file_name, file_component):
                        yield result
                
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")
            logger.info(f"[{group_id}] 初步检查失败，不进行延时复核。")

    async def _check_validity_via_gfs(self, event: AstrMessageEvent, file_id: str) -> bool:
        group_id = int(event.get_group_id())
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            return bool(url_result and url_result.get('url'))
        except Exception:
            return False

    def _get_preview_from_bytes(self, content_bytes: bytes) -> tuple[str, str]:
        try:
            detection = chardet.detect(content_bytes)
            encoding = detection.get('encoding', 'utf-8') or 'utf-8'
            if encoding and detection['confidence'] > 0.7:
                decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                return decoded_text, encoding
            return "", "未知"
        except Exception:
            return "", "未知"
            
    async def _get_preview_from_zip(self, file_path: str) -> tuple[str, str]:
        def _try_unzip(pwd: Optional[str] = None) -> Optional[tuple[bytes, str]]:
            with zipfile.ZipFile(file_path, 'r') as zf:
                if pwd:
                    zf.setpassword(pwd.encode('utf-8'))
                txt_files_garbled = sorted([f for f in zf.namelist() if f.lower().endswith('.txt')])
                if not txt_files_garbled:
                    return None
                first_txt_garbled = txt_files_garbled[0]
                first_txt_fixed = self._fix_zip_filename(first_txt_garbled)
                content_bytes = zf.read(first_txt_garbled)
                return content_bytes, first_txt_fixed

        content_bytes, inner_filename = None, None
        try:
            result = _try_unzip()
            if result: content_bytes, inner_filename = result
        except RuntimeError:
            if self.default_zip_password:
                logger.info(f"无密码解压 '{os.path.basename(file_path)}' 失败，尝试使用默认密码...")
                try:
                    result = _try_unzip(self.default_zip_password)
                    if result: content_bytes, inner_filename = result
                except Exception as e:
                    logger.error(f"使用默认密码解压失败: {e}")
                    return "", ""
            else:
                return "", ""
        except Exception as e:
            logger.error(f"处理ZIP文件时发生未知错误: {e}")
            return "", ""

        if not content_bytes:
            return "", ""

        preview_text, encoding = self._get_preview_from_bytes(content_bytes)
        extra_info = f"已解压「{inner_filename}」(格式 {encoding})"
        return preview_text, extra_info

    async def _get_preview_for_file(self, file_name: str, file_component: Comp.File) -> tuple[str, str]:
        is_txt = file_name.lower().endswith('.txt')
        is_zip = self.enable_zip_preview and file_name.lower().endswith('.zip')
        local_file_path = None
        try:
            if not (is_txt or is_zip):
                return "", ""
            async with self.download_semaphore:
                local_file_path = await file_component.get_file()
            if is_txt:
                with open(local_file_path, 'rb') as f:
                    content_bytes = f.read(2048)
                preview_text, encoding = self._get_preview_from_bytes(content_bytes)
                extra_info = f"格式为 {encoding}"
                return preview_text, extra_info
            if is_zip:
                return await self._get_preview_from_zip(local_file_path)
        except Exception as e:
            logger.error(f"获取预览时下载或读取文件失败: {e}", exc_info=True)
            return "", ""
        finally:
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.remove(local_file_path)
                except OSError as e:
                    logger.warning(f"删除临时文件 {local_file_path} 失败: {e}")
        return "", ""

    async def _task_delayed_recheck(self, event: AstrMessageEvent, file_name: str, file_id: str, file_component: Comp.File, preview_text: str):
        await asyncio.sleep(self.check_delay_seconds)
        group_id = int(event.get_group_id())
        message_id = event.message_obj.message_id
        logger.info(f"[{group_id}] [阶段二] 开始延时复核: '{file_name}'")
        is_still_valid = await self._check_validity_via_gfs(event, file_id)
        if not is_still_valid:
            logger.error(f"❌ [{group_id}] [阶段二] 文件 '{file_name}' 在延时复核时确认已失效!")
            try:
                failure_message = f"❌ 经 {self.check_delay_seconds} 秒后复核，您发送的文件「{file_name}」已失效。"
                await self._send_or_forward(event, failure_message, message_id)
                
                is_txt = file_name.lower().endswith('.txt')
                if self.enable_repack_on_failure and is_txt and preview_text:
                    logger.info("文件在延时复核时失效但内容可读，触发重新打包任务...")
                    async for result in self._repack_and_send_txt(event, file_name, file_component):
                        yield result

            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")
        else:
            logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过，保持沉默。")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")
