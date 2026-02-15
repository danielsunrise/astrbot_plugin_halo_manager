import time
import uuid
import json
import logging
import aiohttp
from typing import List, Optional

from astrbot.api.all import *
from astrbot.core.message.components import Image

# 设置日志
logger = logging.getLogger("astrbot.plugins.halo_manager")

@register(
    "halo_manager",
    "CAN",
    "Halo 2.x 博客管理插件 - 支持发布文章、管理评论、上传素材",
    "1.2.0",
    "https://github.com/your-repo/halo_manager" 
)
class HaloManager(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 从配置中获取参数，如果没有则留空
        self.base_url = self.config.get("halo_url", "").rstrip('/')
        self.token = self.config.get("halo_token", "")
        
        # 检查配置
        if not self.base_url or not self.token:
            logger.warning("Halo Manager 插件未配置 URL 或 Token，请在 AstrBot 后台或配置文件中填写。")

    # ================= 辅助函数：API 请求封装 =================

    async def _request(self, method: str, endpoint: str, json_data: dict = None, form_data: aiohttp.FormData = None) -> dict:
        """统一处理 Halo API 请求"""
        if not self.base_url or not self.token:
            return {"error": "未配置 Halo URL 或 Token，请联系管理员。"}

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }
        # 注意：如果是 FormData (上传图片)，不要手动设置 Content-Type，aiohttp 会处理 boundary

        async with aiohttp.ClientSession() as session:
            try:
                if form_data:
                    # 上传文件
                    async with session.request(method, url, headers=headers, data=form_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            return {"error": f"API Error {resp.status}", "details": text}
                        return await resp.json()
                else:
                    # 普通 JSON 请求
                    headers["Content-Type"] = "application/json"
                    async with session.request(method, url, headers=headers, json=json_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            return {"error": f"API Error {resp.status}", "details": text}
                        return await resp.json()
            except Exception as e:
                logger.error(f"Halo API Request Failed: {e}")
                return {"error": "网络请求异常", "details": str(e)}

    # ================= LLM 工具 (LLM Tools) =================

    @filter.llm_tool(name="publish_blog_post")
    async def publish_post(self, event: AstrMessageEvent, title: str, content: str, slug: str = None):
        """
        发布一篇新的博客文章。
        
        Args:
            title (str): 文章标题
            content (str): 文章正文（Markdown 格式）
            slug (str): (可选) 文章的别名/URL路径。如果不填，系统会自动生成。
        """
        if not slug:
            slug = f"post-{int(time.time())}" # 防止冲突
        
        # Halo 2.x API 结构
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

        # 发送提示
        yield event.plain_result(f"正在发布文章《{title}》... ✍️")

        res = await self._request("POST", "/apis/content.halo.run/v1alpha1/posts", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 发布失败: {res['error']} - {res.get('details', '')}")
        else:
            post_url = f"{self.base_url}/archives/{slug}"
            yield event.plain_result(f"✅ 发布成功！\n🔗 链接: {post_url}")

    @filter.llm_tool(name="get_blog_comments")
    async def get_comments(self, event: AstrMessageEvent):
        """
        获取博客最新的评论列表，用于查看是否有新留言。
        """
        # 获取最新的5条
        endpoint = "/apis/content.halo.run/v1alpha1/comments?sort=metadata.creationTimestamp,desc&page=0&size=5"
        res = await self._request("GET", endpoint)

        if "error" in res:
            yield event.plain_result(f"❌ 获取评论失败: {res['error']}")
            return

        items = res.get("items", [])
        if not items:
            yield event.plain_result("📭 目前没有新的评论。")
            return

        msg_list = ["📝 最新评论："]
        for item in items:
            spec = item.get("spec", {})
            metadata = item.get("metadata", {})
            
            c_id = metadata.get("name")
            c_user = spec.get("owner", {}).get("displayName", "匿名")
            c_content = spec.get("content", "")
            c_post = spec.get("subjectRef", {}).get("name", "未知文章")
            
            msg_list.append(f"--------------\n👤 {c_user}: {c_content}\n🆔 ID: {c_id}\n📄 文章ID: {c_post}")

        msg_list.append("\n💡 提示: 回复请调用 reply_blog_comment")
        yield event.plain_result("\n".join(msg_list))

    @filter.llm_tool(name="reply_blog_comment")
    async def reply_comment(self, event: AstrMessageEvent, comment_id: str, content: str, post_id: str):
        """
        回复博客评论。
        
        Args:
            comment_id (str): 要回复的评论ID。
            content (str): 回复的内容。
            post_id (str): 该评论所属的文章ID。
        """
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
            yield event.plain_result(f"❌ 回复失败: {res['error']}")
        else:
            yield event.plain_result(f"✅ 回复成功！")

    @filter.llm_tool(name="upload_blog_image")
    async def upload_image(self, event: AstrMessageEvent):
        """
        上传图片到博客。
        注意：必须在用户发送图片的消息中调用此工具（或者引用图片）。
        如果当前消息没有图片，工具会报错。
        """
        # 1. 解析消息中的图片
        target_img_url = None
        
        # 遍历消息链寻找图片组件
        for component in event.message_obj.message:
            if isinstance(component, Image):
                target_img_url = component.url
                break
        
        if not target_img_url:
            yield even
