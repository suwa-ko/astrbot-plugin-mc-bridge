"""Local, deterministic MC prompt budgets and stable tool groups."""
from __future__ import annotations
import math
import re

READONLY=frozenset({'mc_status','mc_brief','mc_look','mc_look_at','mc_inspect_target','mc_screenshot','mc_see','mc_scan','mc_inspect_block','mc_block_context','mc_get_menu','mc_capabilities','mc_task_status'})
ACTION=READONLY|{'mc_inventory','mc_goto','mc_come','mc_follow','mc_stop','mc_attack','mc_eat','mc_flee','mc_chat','mc_mine_at','mc_dig_down','mc_mine_below','mc_place_at','mc_use_item','mc_use_bonemeal','mc_release_use','mc_drop_item','mc_select_slot','mc_container','mc_interact','mc_interact_block','mc_align','mc_move_item','mc_use_on_entity','mc_trade','mc_ride','mc_wake','mc_pickup','mc_gather','mc_smelt','mc_container_transfer','mc_build','mc_farm','mc_sleep','mc_trade_for','mc_combat','mc_explore','mc_resupply','mc_memory','mc_wait','mc_unequip_armor','mc_till_block','mc_jump_once','mc_task_start','mc_task_cancel','mc_equip_best','mc_eat_until_full','mc_harvest_block','mc_return_to','mc_plant_at','mc_till_and_plant','mc_open_and_read','mc_place_line','mc_set_respawn'}
SURVIVAL=ACTION|{'mc_equip','mc_craft','mc_recipes'}
MEMORY=re.compile(r'记得|记忆|回忆|经历|聊过|上次|之前|昨天|昨晚|前天|刚才|刚刚|那次|接着|继续|我们.{0,12}(?:做|去|挖|采)|(?:家|工作台|木头).{0,8}(?:在哪|哪里)')
BUILD=re.compile(r'补给|备料|采集|砍|挖|拿|手持|换|穿|戴|镐|斧|木头|原木|木板|木棍|合成|制作|做.{0,8}(?:镐|斧|剑|工具|工作台)|工作台|装备|护甲|整理|背包|睡|生存|建造|种植|种地|小麦|煤|石料|材料|设家|安家|模组|配方|烧炼|熔炉|开门|关门|autonomy|craft|collect|recipe|(?<![a-z])mods?(?![a-z])',re.I)
MOVE=re.compile(r'跟|进屋|出门|回家|靠近|抬头|低头|挪|说一句|说声|发言|mc\s*(?:说|喊|发)|过来|来我|到我|坐标|前往|走|去|移动|停|攻击|揍|打|杀|撤|逃|快跑|吃|看着我|看向|goto|follow|attack|继续|接着|下一步|继续干|接着干',re.I)
ACTION_VERB=re.compile(r'拆|拆掉|打掉|破坏|移除|放|放下|摆放|摆|放置|放进|放到|存入|取出|拿回|搬|搬运|捡|捡起|拾取|用|使用|打开|关闭|关上|按|点|丢|扔|骑|坐|下马|下船|下车|交易|买卖|村民|喂|喂食|拴|命名|攻击|揍|打|杀|砍|挖|向下挖|竖井|采集|收割|收获|探索|探路|侦察|巡逻|扫描|查看|看看|列出|移动|跟随|停|过来|前往|坐标|靠近|回家|出门|进屋|拿起|容器|箱子|背包|起床|醒来|叫醒|建|建造|搭|砌|铺|补给|备货|补充|记住|记下|设为|标记|对齐|松开|等待|脱|卸下|卸|跳|耕地|锄|骨粉|施肥|催熟|启动任务|执行任务|取消任务|跑任务|开始任务|装备最佳|吃满|吃到饱|采集方块|返回坐标|回到坐标|种植|播种|种下|耕地并种|打开容器|读取容器|铺一行|放一行|摆一行|重生点|设重生|设置重生|家床',re.I)
CONFIRM=re.compile(r'^(?:对|对的|好|好的|可以|行|嗯|是的|没错|继续|确认|ok|okay|yes|是)[。.!！?？\s]*$',re.I)
REQUEST=re.compile(r'请|试着|帮我|帮忙|给我|替我|能不能|试试|干活|整点|弄点|继续|接着')

def estimate_tokens(text):
    """Conservative local estimate, not provider billing usage."""
    return math.ceil(len(str(text).encode('utf-8'))/2)

def clip_text(text,budget):
    text=str(text);budget=max(0,int(budget))
    if estimate_tokens(text)<=budget:return text
    marker='…'
    if budget<estimate_tokens(marker):return ''
    lo,hi=0,len(text)
    while lo<hi:
        n=(lo+hi+1)//2;left=(n+1)//2;right=n//2
        candidate=text[:left]+marker+(text[-right:] if right else '')
        if estimate_tokens(candidate)<=budget:lo=n
        else:hi=n-1
    return text[:(lo+1)//2]+marker+(text[-(lo//2):] if lo//2 else '')

def history_budget(config):
    try:return max(256,min(4096,int(config.get('game_history_token_budget',1800))))
    except (ValueError,TypeError):return 1800

def bound_history(lines,budget):
    """Keep recent complete exchanges and chronological order, including names."""
    source=list(lines)[-20:]
    if len(source)%2:source=source[1:]
    kept=[]
    for i in range(len(source)-2,-1,-2):
        pair=source[i:i+2];candidate=pair+kept
        if estimate_tokens('\n'.join(candidate))<=budget:kept=candidate
        elif not kept:
            first=clip_text(pair[0],max(1,(budget-2)*2//3))
            second=clip_text(pair[1],max(1,budget-estimate_tokens(first)-2))
            kept=[first,second]
            break
        else:break
    return kept

def memory_needed(text):return bool(MEMORY.search(str(text)))

def action_intent(text):
    text=str(text)
    return bool(BUILD.search(text) or MOVE.search(text) or ACTION_VERB.search(text))

def is_confirmation(text):
    return bool(CONFIRM.match(str(text).strip()))

def tool_group(text,*,allow_actions=True,owner=False,qq_owner=False):
    text=str(text)
    action=action_intent(text)
    memory_question=bool(re.search(r'(?:记得|回忆|聊过|上次|之前|昨天|昨晚|前天).{0,48}(?:吗|什么|怎么|多少|哪|是否|[?？])',text))
    if not allow_actions or (memory_needed(text) and (memory_question or not action)):
        group='chat';names=set(READONLY)
    elif BUILD.search(text) or (action and REQUEST.search(text)):
        group='survival';names=set(SURVIVAL)
    elif action or MOVE.search(text):
        group='action';names=set(ACTION)
    else:
        group='chat';names=set(READONLY)
    if owner:names.add('mc_recall_memory')
    if qq_owner and re.search(r'忘记|删除|清除|不要记',text):names.add('mc_forget_memory')
    return group,frozenset(names)
