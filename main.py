import asyncio
import os
from typing import List, Dict, Optional
import time
import chardet
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import subprocess
from aiocqhttp.exceptions import ActionFailed

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
    "1.4",
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
        self.enable_duplicate_check: bool = self.config.get("enable_duplicate_check", False)
        
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
    
    async def _search_file_id_by_name(self, event: AstrMessageEvent, file_name: str) -> Optional[str]:
        group_id = int(event.get_group_id())
        self_id = event.get_self_id()
        
        logger.info(f"[{group_id}] 开始文件ID搜索：目标文件名='{file_name}'")
        
        try:
            client = event.bot
            file_list = await client.api.call_action('get_group_root_files', group_id=group_id)
            
            if not isinstance(file_list, dict) or 'files' not in file_list:
                logger.warning("get_group_root_files API调用返回了意料之外的格式。")
                return None
            
            for file_info in file_list.get('files', []):
                if file_info.get('file_name') == file_name:
                    logger.info(f"[{group_id}] 成功匹配！文件ID: {file_info.get('file_id')}")
                    return file_info.get('file_id')
            
            logger.info(f"[{group_id}] 未找到匹配文件。")
            return None
        except Exception as e:
            logger.error(f"[{group_id}] 通过文件名搜索文件ID时出错: {e}", exc_info=True)
            return None

    async def _check_if_file_exists_by_size(self, event: AstrMessageEvent, file_name: str, file_size: int, upload_time: int) -> List[Dict]:
        group_id = int(event.get_group_id())
        
        client = event.bot
        all_files_dict = {}
        folders_to_scan = [{'folder_id': '/', 'folder_name': '根目录'}]
        
        while folders_to_scan:
            current_folder = folders_to_scan.pop(0)
            current_folder_id = current_folder['folder_id']
            current_folder_name = current_folder['folder_name']
            
            try:
                if current_folder_id == '/':
                    result = await client.api.call_action('get_group_root_files', group_id=group_id)
                else:
                    result = await client.api.call_action('get_group_files_by_folder', group_id=group_id, folder_id=current_folder_id, file_count=1000)

                if not isinstance(result, dict):
                    logger.warning(f"[{group_id}] API返回了意料之外的格式。")
                    continue
                
                for file_info in result.get('files', []):
                    file_info['parent_folder_name'] = current_folder_name
                    all_files_dict[file_info.get('file_id')] = file_info
                
                for folder_info in result.get('folders', []):
                    folders_to_scan.append(folder_info)

            except Exception as e:
                logger.error(f"[{group_id}] 遍历文件夹 '{current_folder['folder_name']}' 时出错: {e}", exc_info=True)
        
        logger.info(f"[{group_id}] 遍历完成，共找到 {len(all_files_dict)} 个文件。")
        
        possible_duplicates = []
        for file_info in all_files_dict.values():
            if file_info.get('file_size') == file_size:
                possible_duplicates.append(file_info)

        logger.info(f"[{group_id}] 共找到 {len(possible_duplicates)} 个大小匹配的候选项。")
        
        existing_files = []
        removed_files = []
        
        for f in possible_duplicates:
            file_modify_time = f.get('modify_time')
            
            if file_modify_time is not None:
                file_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_modify_time))
                
                # 比较时间戳
                if file_modify_time == upload_time:
                    removed_files.append(f)
                else:
                    existing_files.append(f)
            else:
                existing_files.append(f)

        if removed_files:
            logger.info(f"[{group_id}] 已从候选项中排除自身文件，共 {len(removed_files)} 个。")
        
        if existing_files:
            logger.info(f"[{group_id}] 最终确认 {len(existing_files)} 个真正的重复文件。")
            for f in existing_files:
                modify_time_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(f.get('modify_time', 0)))
                logger.info(
                    f"  ↳ 文件名: '{f.get('file_name', '未知')}'\n"
                    f"    文件ID: {f.get('file_id', '未知')}\n"
                    f"    大小: {f.get('file_size', '未知')}字节\n"
                    f"    上传者: {f.get('uploader_name', '未知')}\n"
                    f"    修改时间: {modify_time_str}\n"
                    f"    所属文件夹: {f.get('parent_folder_name', '根目录')}"
                )
        else:
            logger.info(f"[{group_id}] 未找到真正的重复文件。")
        
        return existing_files
    
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
                    file_size = data_dict.get("file_size")

                    if isinstance(file_size, str):
                        try:
                            file_size = int(file_size)
                        except ValueError:
                            logger.error(f"无法将文件大小 '{file_size}' 转换为整数，已跳过重复性检查。")
                            file_size = None

                    if file_name and file_id:
                        logger.info(f"【原始方式】成功解析: 文件名='{file_name}', ID='{file_id}'")
                        file_component = self._find_file_component(event)
                        if not file_component:
                            logger.error("致命错误：无法在高级组件中找到对应的File对象！")
                            return
                        
                        if self.enable_duplicate_check and file_size is not None:
                            # 获取文件上传时的精确时间戳
                            upload_time = raw_event_data.get("time", int(time.time()))
                            logger.info(f"[{group_id}] 新上传文件时间戳: {upload_time} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(upload_time))})")

                            existing_files = await self._check_if_file_exists_by_size(event, file_name, file_size, upload_time)
                            if existing_files:
                                if len(existing_files) == 1:
                                    existing_file = existing_files[0]
                                    reply_text = (
                                        f"💡 提醒：您发送的文件「{file_name}」可能与群文件中的「{existing_file.get('file_name', '未知文件名')}」重复。\n"
                                        f"  ↳ 上传者: {existing_file.get('uploader_name', '未知')}\n"
                                        f"  ↳ 修改时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(existing_file.get('modify_time', 0)))}\n"
                                        f"  ↳ 所属文件夹: {existing_file.get('parent_folder_name', '根目录')}"
                                    )
                                    await self._send_or_forward(event, reply_text, event.message_obj.message_id)
                                else:
                                    reply_text = f"💡 提醒：您发送的文件「{file_name}」可能与群文件中以下 {len(existing_files)} 个文件重复：\n"
                                    for idx, file_info in enumerate(existing_files, 1):
                                        reply_text += (
                                            f"\n{idx}. {file_info.get('file_name', '未知文件名')}\n"
                                            f"    ↳ 上传者: {file_info.get('uploader_name', '未知')}\n"
                                            f"    ↳ 修改时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_info.get('modify_time', 0)))}\n"
                                            f"    ↳ 所属文件夹: {file_info.get('parent_folder_name', '根目录')}"
                                        )
                                    await self._send_or_forward(event, reply_text, event.message_obj.message_id)
                                return

                        await self._handle_file_check_flow(event, file_name, file_id, file_component)
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
                await event.send(MessageChain([Plain(f"❌ 重新打包失败，错误信息：\n{error_message}")]))
                return
            
            logger.info(f"文件已重新打包至 {repacked_file_path}，准备发送...")
            
            reply_text = "已为您重新打包为ZIP文件发送："
            file_component_to_send = File(file=repacked_file_path, name=new_zip_name)
            
            await event.send(MessageChain([Plain(reply_text)]))
            
            await event.send(MessageChain([file_component_to_send]))
            
            await asyncio.sleep(2)
            
            new_file_id = await self._search_file_id_by_name(event, new_zip_name)
            
            if new_file_id:
                logger.info(f"新文件发送成功，ID为 {new_file_id}，已加入延时复核队列。")
                asyncio.create_task(self._task_delayed_recheck(event, new_zip_name, new_file_id, file_component, None))
            else:
                logger.error("未能获取新文件的ID，无法进行延时复核。")
            
        except FileNotFoundError:
            logger.error("重新打包失败：容器内未找到 zip 命令。请安装 zip。")
            await event.send(MessageChain([Plain("❌ 重新打包失败。容器内未找到 zip 命令，请联系管理员安装。")]))
        except Exception as e:
            logger.error(f"重新打包并发送文件时出错: {e}", exc_info=True)
            await event.send(MessageChain([Plain("❌ 重新打包并发送文件失败。")]))
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
                    await self._repack_and_send_txt(event, file_name, file_component)
                
            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段一] 回复失效通知时再次发生错误: {send_e}")

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
            
            # 优先使用 chardet 的高置信度结果
            if encoding and detection['confidence'] > 0.7:
                decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                return decoded_text, encoding
            
            # 如果置信度低或无法识别，使用 chardet 猜测的最可能编码进行回退
            if encoding:
                decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                return decoded_text, f"{encoding} (低置信度回退)"
            
            return "", "未知"
            
        except Exception:
            return "", "未知"
            
    async def _get_preview_from_zip(self, file_path: str) -> tuple[str, str]:
        temp_dir = os.path.join(get_astrbot_data_path(), "plugins_data", "file_checker", "temp")
        os.makedirs(temp_dir, exist_ok=True)
        
        extract_path = os.path.join(temp_dir, f"extract_{int(time.time())}")
        os.makedirs(extract_path, exist_ok=True)
        
        extracted_txt_path = None
        
        try:
            # 第一次尝试：无密码解压
            logger.info("正在尝试无密码解压...")
            command_no_pwd = ["7za", "x", file_path, f"-o{extract_path}", "-y"]
            process = await asyncio.create_subprocess_exec(
                *command_no_pwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                # 第一次尝试失败，检查是否有默认密码
                if self.default_zip_password:
                    logger.info("无密码解压失败，正在尝试使用默认密码...")
                    # 第二次尝试：使用默认密码解压
                    command_with_pwd = ["7za", "x", file_path, f"-o{extract_path}", f"-p{self.default_zip_password}", "-y"]
                    process = await asyncio.create_subprocess_exec(
                        *command_with_pwd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    stdout, stderr = await process.communicate()
                    
                    if process.returncode != 0:
                        error_message = stderr.decode('utf-8')
                        logger.error(f"使用默认密码解压失败: {error_message}")
                        return "", "解压失败"
                else:
                    error_message = stderr.decode('utf-8')
                    logger.error(f"使用 7za 命令解压失败且未设置默认密码: {error_message}")
                    return "", "解压失败"

            # 成功解压后，查找 .txt 文件
            all_extracted_files = os.listdir(extract_path)
            txt_files = [f for f in all_extracted_files if f.lower().endswith('.txt')]
            
            if not txt_files:
                return "", "无法找到 .txt 文件"
                
            first_txt_file = txt_files[0]
            extracted_txt_path = os.path.join(extract_path, first_txt_file)
            
            with open(extracted_txt_path, 'rb') as f:
                content_bytes = f.read(2048)
            
            preview_text, encoding = self._get_preview_from_bytes(content_bytes)
            extra_info = f"已解压「{first_txt_file}」(格式 {encoding})"
            return preview_text, extra_info
            
        except FileNotFoundError:
            logger.error("解压失败：容器内未找到 7za 命令。请安装 p7zip-full。")
            return "", "未安装 7za"
        except Exception as e:
            logger.error(f"处理ZIP文件时发生未知错误: {e}", exc_info=True)
            return "", "未知错误"
        finally:
            if extract_path and os.path.exists(extract_path):
                async def cleanup_folder(path: str):
                    await asyncio.sleep(5)
                    try:
                        for item in os.listdir(path):
                            item_path = os.path.join(path, item)
                            if os.path.isfile(item_path):
                                os.remove(item_path)
                        os.rmdir(path)
                        logger.info(f"已清理临时文件夹: {path}")
                    except OSError as e:
                        logger.warning(f"删除临时文件夹 {path} 失败: {e}")
                
                asyncio.create_task(cleanup_folder(extract_path))

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
                    await self._repack_and_send_txt(event, file_name, file_component)

            except Exception as send_e:
                logger.error(f"[{group_id}] [阶段二] 回复失效通知时再次发生错误: {send_e}")
        else:
            logger.info(f"✅ [{group_id}] [阶段二] 文件 '{file_name}' 延时复核通过，保持沉默。")

    async def terminate(self):
        logger.info("插件 [群文件失效检查] 已卸载。")