"""分流和订单抽取共用号码边界，具体手机号格式由原 normalize 节点核验。"""
import re

PHONE_NUMBER = re.compile(r"(?<!\d)1\d{10}(?!\d)")


def phone_numbers(message):
    return tuple(dict.fromkeys(PHONE_NUMBER.findall(message)))
