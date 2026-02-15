import time
import uuid
import json
import aiohttp
from typing import List, Optional, Dict, Any

# 导入所有标准 API
from astrbot.api.all import *
from astrbot.core.message.components import Image

@register(
    "astrbot_plugin_halo_manager",
    "CAN",
    "Halo 2.x 博客管理插件",
    "1.2.7",
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
        
        # 使用 print 代替 logger，彻底规避格式化错误
        if not self.base_url or not self.token:
            print("[HaloManager] ⚠️ 配置缺失！请在 Web 面板或 _conf_schema.json 中填写 URL 和 Token。")

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

        # 捕获所有可能的网络异常
        try:
            async with aiohttp.ClientSession() as session:
                if form_data:
                    # 上传文件通常不需要手动设置 Content-Type，aiohttp 会自动处理 boundary
                    async with session.request(method, url, headers=headers, data=form_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            print(f"[HaloManager] Upload Error: {resp.status} - {text[:100]}")
                            return {"error": f"API Error {resp.status}", "details": text[:200]}
                        return await resp.json()
                else:
                    headers["Content-Type"] = "application/json"
                    async with session.request(method, url, headers=headers, json=json_data) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            print(f"[HaloManager] API Error: {resp.status} - {text[:100]}")
                            return {"error": f"API Error {resp.status}", "details": text[:200]}
                        return await resp.json()
        except Exception as e:
            print(f"[HaloManager] Network Exception: {e}")
            return {"error": "网络请求异常", "details": str(e)}

    # ================= Command / Tools =================
    
    @command("publish_blog_post")
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

        res = await self._request("POST", "/apis/content.halo.run/v1alpha1/posts", json_data=payload)
        
        if "error" in res:
            yield event.plain_result(f"❌ 发布失败: {res.get('details', '未知错误')}")
        else:
            post_url = f"{self.base_url}/archives/{slug}"
            yield event.plain_result(f"✅ 发布成功！\n文章标题: {title}\n🔗 链接: {post_url}")

    @command("get_blog_comments")
    async def get_comments(self, event: AstrMessageEvent):
        """获取博客最新的评论列表"""
        
        # Halo 2.x API
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
            
            # 简单的内容截断
            if len(c_content) > 50: c_content = c_content[:50] + "..."
            
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
        # 1. 获取原评论信息，为了拿到文章 ID (subjectRef)
        info_res = await self._request("GET", f"/apis/content.halo.run/v1alpha1/comments/{comment_id}")
        
        if "error" in info_res:
            yield event.plain_result(f"❌ 找不到原评论 (ID: {comment_id})")
            return
            
        post_id = info_res.get("spec", {}).get("subjectRef", {}).get("name")
        if not post_id:
            yield event.plain_result("❌ 无法解析原评论所属文章，回复失败。")
            return

        # 2. 构造回复 payload
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

    @command("upload_blog_image")
    async def upload_image(self, event: AstrMessageEvent):
        """
        上传图片到博客。
        """
        target_img_url = None
        
        # 解析消息中的图片
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

        # 构造上传表单
        file_name = f"upload_{int(time.time())}.jpg"
        form_data = aiohttp.FormData()
        # 注意：add_field 的参数顺序和字段名要符合 Halo 要求
        form_data.add_field('file', img_bytes, filename=file_name, content_type='image/jpeg')
        form_data.add_field('policy', 'default')
        form_data.add_field('group', 'default')

        # 上传端点
        res = await self._request("POST", "/apis/api.console.halo.run/v1alpha1/attachments/upload", form_data=form_data)

        if "error" in res:
            yield event.plain_result(f"❌ 上传 Halo 失败: {res.get('details')}")
        else:
            permalink = res.get("spec", {}).get("permalink", "")
            yield event.plain_result(f"✅ 上传成功！\n🔗 Link: {permalink}")
