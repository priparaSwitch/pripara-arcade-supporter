"""Native HK rank thresholds, with the original operating-edition cap.

The native table has 39 <2H3I> rows. The regular rank lookup at 0x885D50
compares row+8 against cumulative likes; row+12 belongs to the separate
pre-promotion lookup at 0x885E00 and is not used as the regular threshold.
No installed game, account, network, or private configuration is accessed.
"""
from protocol_delivery import _write_radix255

TABLE_SHA256='c163f782834b2cd5f75576241e07a19b8b879e9f2786bfd4fcb2ff94b53e245e'
MAX_LIKES=255**4-1
# All values come from idol_rank_data.bin; this is public game/protocol data.
THRESHOLDS=(0,5000,30000,60000,100000,200000,300000,450000,600000,
    800000,1000000,1200000,1500000,1800000,2100000,2400000,2700000,
    3000000,3300000,3700000,4200000,4800000,5500000,6300000,7200000,
    8200000,9500000,11000000,13000000,16000000,20000000,24700000,
    30000000,36000000,38000000,40000000,46300000,52800000,59500000)

def maximum_rank(edition=0):
    """Mirror native edition caps; an unverified edition uses the base cap33."""
    if type(edition) is not int or edition<0:raise ValueError('운영 버전 값이 올바르지 않습니다.')
    return 38 if edition>=10 else 37 if edition>=8 else 36 if edition>=6 else 35 if edition>=4 else 34 if edition>=2 else 33

def rank_for_likes(likes,*,edition=0):
    if type(likes) is not int or not 0<=likes<=MAX_LIKES:raise ValueError('좋아요는 0 이상의 범위 안 숫자로 입력해 주세요.')
    rank=0
    for index,threshold in enumerate(THRESHOLDS[:maximum_rank(edition)+1]):
        if threshold>likes:break
        rank=index
    return rank

def rank_profile_for_likes(likes,*,edition=0):
    rank=rank_for_likes(likes,edition=edition)
    return dict(rank=rank,last_rank=rank,rank_up_like_point=likes,likes=likes)

def apply_native_rank(body,likes,*,edition=0):
    """Write coherent current/previous rank and likes; never touch play count."""
    if not isinstance(body,bytearray) or len(body)!=121:raise ValueError('티켓 본문이 올바르지 않습니다.')
    values=rank_profile_for_likes(likes,edition=edition)
    for offset,width,value in ((25,1,values['rank']),(29,4,likes),(33,4,likes),(43,1,values['last_rank'])):
        _write_radix255(body,offset,width,value)
    return values

def correct_missing_rank(body,likes,*,edition=0):
    """Repair the old creator's empty rank without rewriting known progress."""
    from protocol_delivery import read_radix255

    if read_radix255(body,25,1)!=0:return False
    rank=rank_for_likes(likes,edition=edition)
    if not rank:return False
    _write_radix255(body,25,1,rank);_write_radix255(body,43,1,rank)
    return True
