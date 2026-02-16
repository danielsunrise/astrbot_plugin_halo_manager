import json
import re
import time
import uuid
import aiohttp
from typing import Any, Dict, Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

# 导入所有标准 API
from astrbot.api.all import *
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.components import Image

# Halo API 常量
API_CONTENT = "content.halo.run/v1alpha1"
API_CONSOLE = "api.console.halo.run/v1alpha1"


CONFIG_HALO_URL = "halo_url"
CONFIG_HALO_TOKEN = "halo_token"


def _build_create_post_payload(title: str, content: str, slug: str) -> Dict[str, Any]:
    """构建发帖 payload。POST /apis/.../posts 由 ReactiveExtensionClientImpl.create 处理，只接受单个 Post 资源，不能发 content+post 包装。"""
    raw = content or ""
    excerpt_raw = (raw[:500] + "...") if len(raw) > 500 else raw
    return {
        "apiVersion": API_CONTENT,
        "kind": "Post",
        "metadata": {
            "name": slug,
            "labels": {},
        },
        "spec": {
            "title": title or "无标题",
            "slug": slug,
            "visible": "PUBLIC",
            "allowComment": True,
            "excerpt": {"autoGenerate": True, "raw": excerpt_raw},
            "publish": True,
            "deleted": False,
            "pinned": False,
            "priority": 0,
            "template": "",
            "tags": [],
            "categories": [],
            "raw": raw,
            "originalContent": raw,
        },
    }


# ---------- LLM Tools（按文档 https://docs.astrbot.app/dev/star/guides/ai.html#定义-tool 使用 FunctionTool + add_llm_tools 注册） ----------


@dataclass
class PublishBlogPostTool(FunctionTool[AstrAgentContext]):
    """在 Halo 博客上发布一篇新文章。当用户要求发博客、写文章、发布到博客时调用。"""

    plugin: Any = Field(default=None, exclude=True)
    name: str = "publish_blog_post"
    description: str = "在 Halo 博客上发布一篇新文章。当用户要求发博客、写文章、发布到博客时调用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "文章标题。"},
                "content": {"type": "string", "description": "文章正文，支持 Markdown 格式。"},
                "slug": {"type": "string", "description": "可选，URL 路径别名。不填则自动生成。"},
            },
            "required": ["title", "content"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if self.plugin is None:
            return "error: plugin not initialized"
        event = context.context.event
        return await self.plugin._llm_publish_post(
            event,
            title=kwargs.get("title", ""),
            content=kwargs.get("content", ""),
            slug=kwargs.get("slug", ""),
        )


@dataclass
class GetBlogCommentsTool(FunctionTool[AstrAgentContext]):
    """获取 Halo 博客最新的评论列表。当用户问「有什么新评论」「看看评论」时调用。"""

    plugin: Any = Field(default=None, exclude=True)
    name: str = "get_blog_comments"
    description: str = "获取 Halo 博客最新的评论列表。当用户问「有什么新评论」「看看评论」时调用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if self.plugin is None:
            return "error: plugin not initialized"
        event = context.context.event
        return await self.plugin._llm_get_comments(event)


@dataclass
class ReplyBlogCommentTool(FunctionTool[AstrAgentContext]):
    """回复 Halo 博客上的一条评论。当用户要求「回复评论」「回复某条评论」时调用。"""

    plugin: Any = Field(default=None, exclude=True)
    name: str = "reply_blog_comment"
    description: str = "回复 Halo 博客上的一条评论。当用户要求「回复评论」「回复某条评论」时调用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "comment_id": {
                    "type": "string",
                    "description": "要回复的评论的唯一 ID（从 get_blog_comments 可获取）。",
                },
                "content": {"type": "string", "description": "回复内容。"},
            },
            "required": ["comment_id", "content"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if self.plugin is None:
            return "error: plugin not initialized"
        event = context.context.event
        return await self.plugin._llm_reply_comment(
            event,
            comment_id=kwargs.get("comment_id", ""),
            content=kwargs.get("content", ""),
        )


@dataclass
class UploadBlogImageTool(FunctionTool[AstrAgentContext]):
    """将指定图片 URL 的图片上传到 Halo 博客。当用户要求「把这张图发到博客」「上传图片到博客」且提供了图片链接时调用。"""

    plugin: Any = Field(default=None, exclude=True)
    name: str = "upload_blog_image"
    description: str = "将指定图片 URL 的图片上传到 Halo 博客。当用户要求「把这张图发到博客」「上传图片到博客」且提供了图片链接时调用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "图片的完整 URL，需可公网访问。",
                },
            },
            "required": ["image_url"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if self.plugin is None:
            return "error: plugin not initialized"
        event = context.context.event
        return await self.plugin._llm_upload_image(
            event, image_url=kwargs.get("image_url", "")
        )


@register(
    "astrbot_plugin_halo_manager",
    "CAN",
    "Halo 2.x 博客管理插件",
    "1.2.8",
    "https://github.com/danielsunrise/astrbot_plugin_halo_manager"
)
class HaloManager(Star):
    def __init__(self, context: Context, config: Dict[str, Any]):
        super().__init__(context)
        
        self.config = config or {}
        raw_url = self.config.get(CONFIG_HALO_URL, "")
        self.base_url = raw_url.rstrip("/") if raw_url else ""
        self.token = self.config.get(CONFIG_HALO_TOKEN, "")
        if not self.base_url or not self.token:
            logger.warning("配置缺失！请在 Web 面板或 _conf_schema.json 中填写 URL 和 Token。")
        # 按文档在 __init__ 中注册 LLM 工具，供 AI 对话时自动调用
        self.context.add_llm_tools(
            PublishBlogPostTool(plugin=self),
            GetBlogCommentsTool(plugin=self),
            ReplyBlogCommentTool(plugin=self),
            UploadBlogImageTool(plugin=self),
        )

    # ================= 辅助函数 =================

    async def _request(self, method: str, endpoint: str, json_data: Optional[dict] = None, form_data: Optional[aiohttp.FormData] = None) -> dict:
        """异步请求 Halo API"""
        if not self.base_url or not self.token:
            return {"error": "配置未填写", "details": "请在 AstrBot 设置中配置 Halo URL 和 Token"}

        url = f"{self.base_url}{endpoint}"
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json"
        }

        try:
            async with aiohttp.ClientSession() as session:
                req_headers = dict(headers)
                if not form_data:
                    req_headers["Content-Type"] = "application/json"
                req_kw: Dict[str, Any] = {"method": method, "url": url, "headers": req_headers}
                if form_data:
                    req_kw["data"] = form_data
                elif json_data is not None:
                    req_kw["json"] = json_data
                async with session.request(**req_kw) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        logger.warning("API Error %s: %s", resp.status, text[:100])
                        return {"error": f"API Error {resp.status}", "details": text[:200]}
                    try:
                        return json.loads(text) if text.strip() else {}
                    except ValueError:
                        logger.warning("Invalid JSON response: %s", text[:100])
                        return {"error": "响应非 JSON", "details": text[:200]}
        except Exception as e:
            logger.exception("网络请求异常: %s", e)
            return {"error": "网络请求异常", "details": str(e)}

    # ================= Command / Tools =================
    
    @command("publish_blog_post")
    async def publish_post(self, event: AstrMessageEvent, title: str, content: str, slug: Optional[str] = None):
        """
        发布一篇新的博客文章。
        Args:
            title (str): 文章标题
            content (str): 文章正文（Markdown 格式）
            slug (str): (可选) URL路径别名
        """
        if not slug:
            slug = f"post-{int(time.time())}"
        # 仅保留 Halo 支持的字符，避免非法 name/slug
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-") or f"post-{int(time.time())}"
        payload = _build_create_post_payload(title=title, content=content, slug=slug)
        res = await self._request("POST", f"/apis/{API_CONTENT}/posts", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 发布失败: {res.get('details', '未知错误')}")
        else:
            post_url = f"{self.base_url}/archives/{slug}"
            yield event.plain_result(f"✅ 发布成功！\n文章标题: {title}\n🔗 链接: {post_url}")

    @command("get_blog_comments")
    async def get_comments(self, event: AstrMessageEvent):
        """获取博客最新的评论列表"""
        
        endpoint = f"/apis/{API_CONTENT}/comments?sort=metadata.creationTimestamp,desc&page=0&size=5"
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
            
            if len(c_content) > 50:
                c_content = c_content[:50] + "..."
            
            msg_list.append(f"--------------\n👤 {c_user}: {c_content}\n🆔 ID: {c_name_id}")

        msg_list.append("\n💡 让 AI 回复请说: '帮我回复评论 [ID] 内容...'")
        yield event.plain_result("\n".join(msg_list))

    @command("reply_blog_comment")
    async def reply_comment(self, event: AstrMessageEvent, comment_id: str, content: str):
        """
        回复博客评论 (自动查找关联文章)
        Args:
            comment_id (str): 评论的唯一 ID (name)
            content (str): 回复内容
        """
        info_res = await self._request("GET", f"/apis/{API_CONTENT}/comments/{comment_id}")
        
        if "error" in info_res:
            yield event.plain_result(f"❌ 找不到原评论 (ID: {comment_id})")
            return
            
        post_id = info_res.get("spec", {}).get("subjectRef", {}).get("name")
        if not post_id:
            yield event.plain_result("❌ 无法解析原评论所属文章，回复失败。")
            return

        reply_uuid = str(uuid.uuid4())
        payload = {
            "apiVersion": API_CONTENT,
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

        res = await self._request("POST", f"/apis/{API_CONTENT}/comments", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 回复失败: {res.get('details', '未知错误')}")
        else:
            yield event.plain_result(f"✅ 回复成功！")

    # ================= LLM Tool 实现（由 FunctionTool.call 调用） =================

    async def _llm_publish_post(
        self,
        event: AstrMessageEvent,
        title: str,
        content: str,
        slug: str = "",
    ) -> str:
        slug = slug.strip() if slug else f"post-{int(time.time())}"
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-") or f"post-{int(time.time())}"
        payload = _build_create_post_payload(title=title, content=content, slug=slug)
        res = await self._request("POST", f"/apis/{API_CONTENT}/posts", json_data=payload)
        if "error" in res:
            return f"发布失败: {res.get('details', '未知错误')}"
        post_url = f"{self.base_url}/archives/{slug}"
        return f"发布成功。文章标题: {title}，链接: {post_url}"

    async def _llm_get_comments(self, event: AstrMessageEvent) -> str:
        endpoint = f"/apis/{API_CONTENT}/comments?sort=metadata.creationTimestamp,desc&page=0&size=5"
        res = await self._request("GET", endpoint)
        if "error" in res:
            return f"获取失败: {res['error']}"
        items = res.get("items", [])
        if not items:
            return "暂无新评论。"
        lines = ["最新 5 条评论："]
        for item in items:
            spec = item.get("spec", {})
            metadata = item.get("metadata", {})
            c_name_id = metadata.get("name")
            c_user = spec.get("owner", {}).get("displayName", "匿名用户")
            c_content = spec.get("content", "无内容")
            if len(c_content) > 50:
                c_content = c_content[:50] + "..."
            lines.append(f"用户 {c_user}: {c_content}，评论 ID: {c_name_id}")
        return "\n".join(lines)

    async def _llm_reply_comment(
        self,
        event: AstrMessageEvent,
        comment_id: str,
        content: str,
    ) -> str:
        info_res = await self._request("GET", f"/apis/{API_CONTENT}/comments/{comment_id}")
        if "error" in info_res:
            return f"找不到原评论 (ID: {comment_id})"
        post_id = info_res.get("spec", {}).get("subjectRef", {}).get("name")
        if not post_id:
            return "无法解析原评论所属文章，回复失败。"
        payload = {
            "apiVersion": API_CONTENT,
            "kind": "Comment",
            "metadata": {"name": str(uuid.uuid4())},
            "spec": {
                "content": content,
                "subjectRef": {
                    "group": "content.halo.run",
                    "kind": "Post",
                    "name": post_id,
                    "version": "v1alpha1",
                },
                "parentId": comment_id,
            },
        }
        res = await self._request("POST", f"/apis/{API_CONTENT}/comments", json_data=payload)
        if "error" in res:
            return f"回复失败: {res.get('details', '未知错误')}"
        return "回复成功。"

    async def _llm_upload_image(
        self,
        event: AstrMessageEvent,
        image_url: str,
    ) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        return "无法下载图片源文件。"
                    img_bytes = await resp.read()
        except Exception as e:
            return f"下载异常: {e}"
        file_name = f"upload_{int(time.time())}.jpg"
        form_data = aiohttp.FormData()
        form_data.add_field("file", img_bytes, filename=file_name, content_type="image/jpeg")
        form_data.add_field("policy", "default")
        form_data.add_field("group", "default")
        res = await self._request(
            "POST", f"/apis/{API_CONSOLE}/attachments/upload", form_data=form_data
        )
        if "error" in res:
            return f"上传 Halo 失败: {res.get('details', '未知错误')}"
        permalink = res.get("spec", {}).get("permalink", "")
        return f"上传成功，链接: {permalink}"

    @command("upload_blog_image")
    async def upload_image(self, event: AstrMessageEvent):
        """
        上传图片到博客。
        """
        target_img_url = None
        
        for component in event.message_obj.message:
            if isinstance(component, Image):
                target_img_url = component.url
                break
        
        if not target_img_url:
            yield event.plain_result("⚠️ 请发送包含图片的指令。")
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

        res = await self._request("POST", f"/apis/{API_CONSOLE}/attachments/upload", form_data=form_data)

        if "error" in res:
            yield event.plain_result(f"❌ 上传 Halo 失败: {res.get('details', '未知错误')}")
        else:
            permalink = res.get("spec", {}).get("permalink", "")
            yield event.plain_result(f"✅ 上传成功！\n🔗 Link: {permalink}")
