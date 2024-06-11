import requests
import re
from lxml import etree
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from plugins import Plugin, Event, EventContext, EventAction
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from bilibili_api import video, sync
import plugins


import sys
import time

CRCPOLYNOMIAL = 0xEDB88320
crctable = [0 for x in range(256)]

for i in range(256):
    crcreg = i
    for _ in range(8):
        if (crcreg & 1) != 0:
            crcreg = CRCPOLYNOMIAL ^ (crcreg >> 1)
        else:
            crcreg = crcreg >> 1
    crctable[i] = crcreg

def crc32(text):
    crcstart = 0xFFFFFFFF
    for i in range(len(str(text))):
        index = (crcstart ^ ord(str(text)[i])) & 255
        crcstart = (crcstart >> 8) ^ crctable[index]
    return crcstart

def crc32_last_index(text):
    crcstart = 0xFFFFFFFF
    for i in range(len(str(text))):
        index = (crcstart ^ ord(str(text)[i])) & 255
        crcstart = (crcstart >> 8) ^ crctable[index]
    return index

def get_crc_index(t):
    for i in range(256):
        if crctable[i] >> 24 == t:
            return i
    return -1

def deep_check(i, index):
    text = ""
    tc=0x00
    hashcode = crc32(i)
    tc = hashcode & 0xff ^ index[2]
    if not (tc <= 57 and tc >= 48):
        return [0]
    text += str(tc - 48)
    hashcode = crctable[index[2]] ^ (hashcode >>8)
    tc = hashcode & 0xff ^ index[1]
    if not (tc <= 57 and tc >= 48):
        return [0]
    text += str(tc - 48)
    hashcode = crctable[index[1]] ^ (hashcode >> 8)
    tc = hashcode & 0xff ^ index[0]
    if not (tc <= 57 and tc >= 48):
        return [0]
    text += str(tc - 48)
    hashcode = crctable[index[0]] ^ (hashcode >> 8)
    return [1, text]

def crack(text):
    index = [0 for x in range(4)]
    i = 0
    ht = int(f"0x{text}", 16) ^ 0xffffffff
    for i in range(3,-1,-1):
        index[3-i] = get_crc_index(ht >> (i*8))
        snum = crctable[index[3-i]]
        ht ^= snum >> ((3-i)*8)
    for i in range(100000000):
        lastindex = crc32_last_index(i)
        if lastindex == index[3]:
            deepCheckData = deep_check(i, index)
            if deepCheckData[0]:
                break
    if i == 100000000:
        return -1
    return f"{i}{deepCheckData[1]}"


@plugins.register(name="BilibiliDanmaku",
                  desc="查询B站视频弹幕",
                  version="1.0",
                  author="YourName",
                  desire_priority=100)
class BilibiliDanmaku(Plugin):
    content = None

    def __init__(self):
        super().__init__()
        self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context
        logger.info(f"[{__class__.__name__}] inited")

    def get_help_text(self, **kwargs):
        help_text = "发送【弹幕 BV号 关键词】获取B站视频弹幕"
        return help_text

    def on_handle_context(self, e_context: EventContext):
        if e_context['context'].type != ContextType.TEXT:
            return
        self.content = e_context["context"].content.strip()

        if self.content.startswith("弹幕"):
            parts = self.content.split(" ")
            if len(parts) != 3:
                reply = Reply()
                reply.type = ReplyType.ERROR
                reply.content = "输入格式错误，请确保格式为：弹幕 BV号 关键词，中间只允许一个空格。"
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
                return

            bv_id = parts[1]
            keyword = parts[2]
            logger.info(f"[{__class__.__name__}] 收到消息: {self.content}")

            reply = Reply()
            result = self.query_danmaku(bv_id, keyword)
            if result is not None:
                reply.type = ReplyType.TEXT
                reply.content = result
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS
            else:
                reply.type = ReplyType.ERROR
                reply.content = "获取弹幕失败，请稍后再试。"
                e_context["reply"] = reply
                e_context.action = EventAction.BREAK_PASS

    def convert_seconds_to_hms(self, seconds):
        """将秒数转换为hh:mm:ss格式"""
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"

    def format_danmaku(self, user_id, dm_text, dm_time_str, send_time_str):
        """格式化弹幕信息"""
        user_homepage = f"https://space.bilibili.com/{user_id}"
        return (
            f"👤 用户ID: {user_id}\n"
            f"🏠 用户主页: {user_homepage}\n"
            f"💬 弹幕内容: {dm_text}\n"
            f"🕒 弹幕出现时间: {dm_time_str}\n"
            f"📅 发送时间: {send_time_str}"
        )

    def process_danmaku(self, dm, keyword):
        """处理单个弹幕的函数"""
        if not dm['text']:  # 过滤掉弹幕内容为空的数据
            return None

        if keyword and keyword not in dm['text']:  # 过滤不包含关键词的弹幕
            return None

        try:
            user_id = crack(dm['crc32_id'])
            dm_time_str = self.convert_seconds_to_hms(dm['dm_time'])
            send_time = datetime.datetime.fromtimestamp(dm['send_time'])
            send_time_str = send_time.strftime('%Y-%m-%d %H:%M:%S')

            return self.format_danmaku(user_id, dm['text'], dm_time_str, send_time_str)
        except Exception as e:
            logger.error(f"Error processing danmaku: {str(e)}")
            return None

    def query_video_info(self, bv_id):
        v = video.Video(bv_id)
        try:
            info = sync(v.get_info())
            video_info = (
                f"🎥 视频标题: {info['title']}\n"
                f"👤 UP主: {info['owner']['name']}\n"
                f"👀 播放数: {info['stat']['view']}\n"
                f"💬 评论数: {info['stat']['reply']}\n"
                f"👍 点赞数: {info['stat']['like']}\n"
                f"💰 投币数: {info['stat']['coin']}\n"
            )
            return f"当前查询视频信息【{bv_id}】如下:\n{video_info}"
        except Exception as e:
            logger.error(f"Error fetching video info: {str(e)}")
            return f"当前查询视频信息【{bv_id}】如下:\n视频信息查询异常"

    def query_danmaku(self, bv_id, keyword=None):
        # 查询视频信息
        video_info = self.query_video_info(bv_id)

        bvid_url = f'https://www.bilibili.com/video/{bv_id}'
        bvid = re.findall(r"video/(\S+)", bvid_url, re.S)[0]
        oid_url = f"https://api.bilibili.com/x/player/pagelist?bvid={bvid}"
        headers = {
            "User-Agent": 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.61 Safari/537.36',
            "referer": f"https://www.bilibili.com/video/{bv_id}"
        }

        response = requests.get(oid_url, headers=headers).content.decode()
        oid = re.findall(r'"cid":(.*?),', response, re.S)[0]
        danmu_url = f"https://api.bilibili.com/x/v1/dm/list.so?oid={oid}"
        response = requests.get(danmu_url, headers=headers).content

        html = etree.HTML(response)
        d_elements = html.xpath("//d")

        dms = []
        for d in d_elements:
            p_attr = d.attrib.get('p')
            text = d.text
            p_attr_parts = p_attr.split(',')
            dm_time = float(p_attr_parts[0])
            send_timestamp = int(p_attr_parts[4])
            crc32_id = p_attr_parts[6]
            dms.append({
                'dm_time': dm_time,
                'send_time': send_timestamp,
                'crc32_id': crc32_id,
                'text': text
            })

        # 按照弹幕时间升序排序
        dms.sort(key=lambda dm: dm['dm_time'])

        results = []
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(self.process_danmaku, dm, keyword) for dm in dms]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)

        danmaku_count = len(results)
        danmaku_results = "\n\n".join(results) if results else "没有找到相关弹幕"
        return f"{video_info}\n\n当前查询弹幕条数【{danmaku_count}】条,信息如下:\n{danmaku_results}"


if __name__ == "__main__":
    bilibili_danmaku_plugin = BilibiliDanmaku()
    bv_id = "BV1AV411x7Gs"  # 示例BV号
    keyword = "恭喜"  # 示例关键词
    result = bilibili_danmaku_plugin.query_danmaku(bv_id, keyword)
    if result:
        print("获取到的信息：\n", result)
    else:
        print("获取失败")
