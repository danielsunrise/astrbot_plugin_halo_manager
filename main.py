import time
import uuid
import json
import logging
import aiohttp
from typing import List, Optional, Dict, Any

# 导入所有标准 API
from astrbot.api.all import *
# 显式导入图片组件，用于处理图片上传
from astrbot.core.message.components import Image

logger = logging.getLogger("astrbot.plugins.halo_manager")

@register(
    "halo_manager",
    "CAN",
    "Halo 2.x 博客管理插件 - 支持发布文章、管理评论、上传素材",
    "1.2.2",
    "https://github.com/your-repo/halo_manager" 
)
class HaloManager(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        self.config = config
        
        # 容错处理：处理 URL 末尾的斜杠
        raw_url = self.config.get("halo_url", "")
        self.base_url = raw_url.rstrip('/') if raw_url else ""
        self.token = self.config.get("halo_token", "")
        
        if not self.base_url or not self.token:
            logger.warning("[HaloManager] ⚠️ 配置缺失！请在 Web 面板或 metadata.yaml 中填写 URL 和 Token。")

    # ================= 辅助函数 =================

    async def _request(self, method: str, endpoint: str, json_data: dict = None, form_data: aiohttp.FormData = None) -> dict:
        """异步请求 Halo API"""
        if not self.base_url or not self.token:
            return {"error": "配置未填写", "details": "请在 AstrBot 设置中配置 Halo URL 和 Token"}

        url = f"{self.base_url}{endpoint}"
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

        async with aiohttp.ClientSession() as session:
            try:
                if form_data:
                    # 上传图片时，Content-Type 由 aiohttp 自动生成 boundary
                    async with session.request(method, url, headers=headers, data=form_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            logger.error(f"[HaloManager] Upload Error: {resp.status} - {text}")
                            return {"error": f"API Error {resp.status}", "details": text[:200]}
                        return await resp.json()
                else:
                    headers["Content-Type"] = "application/json"
                    async with session.request(method, url, headers=headers, json=json_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            logger.error(f"[HaloManager] API Error: {resp.status} - {text}")
                            return {"error": f"API Error {resp.status}", "details": text[:200]}
                        return await resp.json()
            except Exception as e:
                logger.error(f"[HaloManager] Network Exception: {e}")
                return {"error": "网络请求异常", "details": str(e)}

    # ================= 核心功能 (Commands/Tools) =================
    
    # 修正说明：将 @filter.llm_tool 改为 @filter.command
    # AstrBot 会自动解析函数签名和 docstring 作为 LLM 的 Tool 描述

    @filter.command("publish_blog_post")
    async def publish_post(self, event: AstrMessageEvent, title: str, content: str, slug: str = None):
        """
        发布一篇新的博客文章。
        Args:
            title (str): 文章标题
            content (str): 文章正文（Markdown 格式）
            slug (str): (可选) URL路径别名
        """
        if not slug:
            slug = f"post-{int(time.time())}"
        
        payload = {
            "apiVersion": "content.halo.run/v1alpha1",
            "kind": "Post",
            "metadata": {
                "name": slug,
                "labels": {}
            },
            "spec": {
                "title": title,
                "slug": slug,
                "visible": "PUBLIC", 
                "allowComment": True,
                "raw": content,
                "originalContent": content
            }
        }

        # 提示用户正在处理
        # await event.send(f"正在发布文章《{title}》...") 

        res = await self._request("POST", "/apis/content.halo.run/v1alpha1/posts", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 发布失败: {res.get('details', '未知错误')}")
        else:
            post_url = f"{self.base_url}/archives/{slug}"
            yield event.plain_result(f"✅ 发布成功！\n文章标题: {title}\n🔗 链接: {post_url}")

    @filter.command("get_blog_comments")
    async def get_comments(self, event: AstrMessageEvent):
        """获取博客最新的评论列表"""
        
        endpoint = "/apis/content.halo.run/v1alpha1/comments?sort=metadata.creationTimestamp,desc&page=0&size=5"
        res = await self._request("GET", endpoint)

        if "error" in res:
            yield event.plain_result(f"❌ 获取失败: {res['error']}")
            return

        items = res.get("items", [])
        if not items:
            yield event.plain_result("📭 暂无新评论。")
            return

        msg_list = ["📝 最新 5 条评论："]
        for item in items:
            spec = item.get("spec", {})
            metadata = item.get("metadata", {})
            
            c_name_id = metadata.get("name")
            c_user = spec.get("owner", {}).get("displayName", "匿名用户")
            c_content = spec.get("content", "无内容")
            
            if len(c_content) > 50: c_content = c_content[:50] + "..."
            
            msg_list.append(f"--------------\n👤 {c_user}: {c_content}\n🆔 ID: {c_name_id}")

        msg_list.append("\n💡 回复格式: '回复评论 [ID] 内容...' (请让AI调用 reply_blog_comment)")
        yield event.plain_result("\n".join(msg_list))

    @filter.command("reply_blog_comment")
    async def reply_comment(self, event: AstrMessageEvent, comment_id: str, content: str):
        """
        回复博客评论 (自动查找关联文章)
        Args:
            comment_id (str): 评论的唯一 ID (name)
            content (str): 回复内容
        """
        info_res = await self._request("GET", f"/apis/content.halo.run/v1alpha1/comments/{comment_id}")
        
        if "error" in info_res:
            yield event.plain_result(f"❌ 找不到原评论 (ID: {comment_id})")
            return
            
        post_id = info_res.get("spec", {}).get("subjectRef", {}).get("name")
        if not post_id:
            yield event.plain_result("❌ 无法解析原评论所属文章，回复失败。")
            return

        reply_uuid = str(uuid.uuid4())
        payload = {
            "apiVersion": "content.halo.run/v1alpha1",
            "kind": "Comment",
            "metadata": {"name": reply_uuid},
            "spec": {
                "content": content,
                "subjectRef": {
                    "group": "content.halo.run",
                    "kind": "Post",
                    "name": post_id,
                    "version": "v1alpha1"
                },
                "parentId": comment_id
            }
        }

        res = await self._request("POST", "/apis/content.halo.run/v1alpha1/comments", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 回复失败: {res.get('details')}")
        else:
            yield event.plain_result(f"✅ 回复成功！")

    @filter.command("upload_blog_image")
    async def upload_image(self, event: AstrMessageEvent):
        """
        上传图片到博客。必须在发送图片时调用，或引用图片消息。
        """
        target_img_url = None
        
        # 检查当前消息链
        for component in event.message_obj.message:
            if isinstance(component, Image):
                target_img_url = component.url
                break
        
        if not target_img_url:
            yield event.plain_result("⚠️ 请先发送图片（或引用图片），然后说 '上传这张图'。")
            return

        yield event.plain_result("⏳ 正在下载并上传...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(target_img_url) as resp:
                    if resp.status != 200:
                        yield event.plain_result("❌ 无法下载图片源文件。")
                        return
                    img_bytes = await resp.read()
        except Exception as e:
            yield event.plain_result(f"❌ 下载异常: {e}")
            return

        file_name = f"upload_{int(time.time())}.jpg"
        form_data = aiohttp.FormData()
        form_data.add_field('file', img_bytes, filename=file_name, content_type='image/jpeg')
        form_data.add_field('policy', 'default')
        form_data.add_field('group', 'default')

        res = await self._request("POST", "/apis/api.console.halo.run/v1alpha1/attachments/upload", form_data=form_data)

        if "error" in res:
            yield event.plain_result(f"❌ 上传 Halo 失败: {res.get('details')}")
        else:
            permalink = res.get("spec", {}).get("permalink", "")
            yield event.plain_result(f"✅ 上传成功！\n🔗 Markdown: ![]({permalink})")
