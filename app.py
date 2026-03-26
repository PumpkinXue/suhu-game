import os
import json
import uuid
import time
import threading
import queue
import re
from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from datetime import timedelta
import requests

app = Flask(__name__)
# 使用固定secret_key确保session持久化
app.secret_key = os.environ.get("SECRET_KEY", "suhu-game-secret-key-2024")
# 配置session过期时间
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# 任务存储（用于后台执行大模型调用）
# 结构: { task_id: { "status": "pending"|"running"|"done"|"error", "result": {}, "error": None } }
tasks = {}
tasks_lock = threading.Lock()

# DeepSeek API配置（优先使用环境变量）
API_KEY = os.environ.get("API_KEY", "sk-cfc36fb7d5d94dd48c48dce8fde9eef2")
BASE_URL = os.environ.get("BASE_URL", "https://api.deepseek.com/v1")

def call_deepseek_stream(prompt, system_prompt="You are a helpful assistant."):
    """调用DeepSeek API（流式版本）"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.8,
        "stream": True
    }

    response = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=data, stream=True, timeout=120)

    if response.status_code == 200:
        for chunk in response.iter_lines():
            if chunk:
                decoded = chunk.decode('utf-8')
                if decoded.startswith('data: '):
                    data_str = decoded[6:]
                    if data_str == '[DONE]':
                        break
                    try:
                        delta = json.loads(data_str)['choices'][0]['delta'].get('content', '')
                        if delta:
                            yield delta
                    except:
                        pass
    else:
        raise Exception(f"API调用失败: {response.text}")


def call_deepseek(prompt, system_prompt="You are a helpful assistant."):
    """调用DeepSeek API（非流式版本）"""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 1.2
    }

    response = requests.post(f"{BASE_URL}/chat/completions", headers=headers, json=data, timeout=120)

    if response.status_code == 200:
        result = response.json()
        return result['choices'][0]['message']['content']
    else:
        raise Exception(f"API调用失败: {response.text}")

# ========== 任务队列相关函数 ==========

def create_task():
    """创建一个新任务，返回task_id"""
    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            "status": "pending",
            "result": None,
            "error": None
        }
    return task_id

def _get_task_result_internal(task_id):
    """获取任务结果（内部使用）"""
    with tasks_lock:
        if task_id not in tasks:
            return None
        task = tasks[task_id]
        status = task["status"]
        if status == "done":
            result = task["result"]
            del tasks[task_id]
            return {"status": "done", "result": result}
        elif status == "error":
            error = task.get("error", "未知错误")
            del tasks[task_id]
            return {"status": "error", "error": error}
        else:
            return {"status": status}

def set_task_done(task_id, result):
    """设置任务完成"""
    with tasks_lock:
        tasks[task_id]["result"] = result
        tasks[task_id]["status"] = "done"

def set_task_error(task_id, error):
    """设置任务错误"""
    with tasks_lock:
        tasks[task_id]["error"] = error
        tasks[task_id]["status"] = "error"

def run_task_in_background(task_id, target_func, *args):
    """在后台线程中运行任务"""
    def run():
        try:
            result = target_func(*args)
            set_task_done(task_id, result)
        except Exception as e:
            print(f"任务执行失败: {e}")
            set_task_error(task_id, str(e))
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()

# ========== init_game 任务函数 ==========

def generate_game_content(emperor_name, gender, concubine_count, heir_count, background):
    """调用大模型生成游戏初始内容"""
    system_prompt = """你是一个专业的游戏世界观设计师，擅长创建沉浸式的角色扮演游戏内容。
你的输出必须是有效的JSON格式，不要包含任何其他内容。"""

    # 妃子品级和皇嗣品级说明
    concubine_ranks = "正一品(皇后)、从一品(皇贵妃)、正二品(贵妃)、从二品(德妃)、正三品(贤妃)、从三品(丽妃)、正四品(妃)、从四品(嫔)、正五品(贵人)、从五品(常在)、正六品(答应)、从六品(秀女)、正七品及以下(奴婢)"
    male_heir_ranks = "正一品(皇太子)、从一品(亲王)、正二品(郡王)、从二品(贝勒)、正三品(贝子)、从三品(爱子)"
    female_heir_ranks = "正一品(固伦公主)、从一品(和碩公主)、正二品(郡主)、从二品(县主)、正三品(郡君)、从三品(县君)"

    prompt = f"""请为「皇帝后宫模拟器」生成初始游戏数据。

玩家信息：
- 皇帝姓名：{emperor_name}
- 性别：{gender}
- 妃子数量：{concubine_count}人
- 皇嗣数量：{heir_count}人
- 人物背景：{background}

请生成以下JSON内容（必须严格遵循JSON格式）：

{{
    "emperor": {{
        "name": "皇帝姓名",
        "gender": "男/女",
        "background": "人物背景（50字内）",
        "talent": 数值(1-100),
        "martial": 数值(1-100),
        "appearance": 数值(1-100),
        "morality": 数值(1-100)
    }},
    "characters": [
        {{
            "name": "人物姓名",
            "type": "妃嫔/皇嗣",
            "gender": "男/女（妃嫔必填女，皇嗣根据实际填写）",
            "intro": "简介（50字以内，妃子要包括她的身份（如正几品什么什么官员之嫡女/庶女/妹妹，或是特殊身份歌女、奴婢）和她与皇上的相识，皇嗣要包含一个模糊的年龄段，以及性格描述）",
            "personality": "性格描述",
            "mood": "心境",
            "thought": "对目前局势和皇帝的看法（50字以内）",
            "mother": "生母姓名（仅皇嗣填写，妃嫔填无，也可以有特殊生母的皇嗣，这时就要输出未记载/已故/奴婢）",
            "rank": "品级名称（妃嫔从以下列表选择：{concubine_ranks}；皇嗣男选：{male_heir_ranks}，女选：{female_heir_ranks}）",
            "favorability": 数值(-100到100,初始50左右表示初始有好感),
            "sincerity": 数值(-100到100,初始30左右表示初步信任)
        }}
    ]
}}

要求：
1. 皇帝的属性（才华、武力、容貌、道德）根据人物背景合理分配，总和为200-300之间，皇帝的人物背景不能照搬原话，必须要在原话的基础上丰富出一个故事背景。
2. 妃子数量为{concubine_count}人，type为"妃嫔"，男皇帝则gender填"女"，女皇帝则gender填"男"，每个要有rank(品级)、favorability(好感度)、sincerity(真心度)字段
3. 皇嗣数量为{heir_count}人，type为"皇嗣"，gender根据实际填写（男/女），每个要有rank(品级)、favorability、sincerity字段
4. 妃子的rank必须从给定的品级列表中选择，不能自己编造
5. 皇嗣的rank必须根据性别从对应列表中选择，幼年孩童的品级不应该超过正三品
6. 好感度和真心度数值要合理，初始值可以根据人设合理设置正负大小
7. 妃子的名字必须是姓+名，不能是高贵妃，高夫人等姓加称呼。
8. 想法（thought）是对目前局势的看法和对皇帝的看法的综合，50字以内
9. 所有人物都放在characters数组中，不要单独生成concubines和heirs
10. 输出必须是纯JSON，不要有任何其他文字"""

    result = call_deepseek(prompt, system_prompt)

    # 尝试解析JSON
    try:
        # 尝试提取JSON部分
        start = result.find('{')
        end = result.rfind('}') + 1
        if start != -1 and end != 0:
            json_str = result[start:end]
            return json.loads(json_str)
    except json.JSONDecodeError:
        # 如果解析失败，返回模拟数据
        pass

    # 返回备用数据
    return generate_fallback_data(emperor_name, gender, concubine_count, heir_count, background)

def generate_fallback_data(emperor_name, gender, concubine_count, heir_count, background):
    """生成备用数据（当API调用失败时）"""
    import random

    total_points = random.randint(200, 300)
    talents = [random.randint(30, 80) for _ in range(4)]
    while sum(talents) < total_points - 20 or sum(talents) > total_points + 20:
        talents = [random.randint(20, 80) for _ in range(4)]

    names = ["婉儿", "甄嬛", "华妃", "皇后", "貂蝉", "西施", "王昭君", "杨贵妃", "武则天", "太平公主"]
    personalities = ["温柔贤淑", "活泼可爱", "端庄大方", "阴险狡诈", "才情横溢", "天真浪漫", "成熟稳重", "刁蛮任性"]
    concubine_intros = [
        "出身名门望族的青年女子，自幼受到良好教育",
        "原本是普通人家因选秀入宫的青年女子",
        "前朝重臣之女，政治联姻的青年女子",
        "才华横溢的才女，被皇帝赏识的青年女子",
        "端庄秀丽的年轻女子，后宫新秀"
    ]
    moods = ["期待得到皇帝宠爱", "思念家乡亲人", "与其他妃子明争暗斗", "安心享受后宫生活"]
    thoughts = [
        "希望能够得到皇帝的青睐，稳固自己在后宫的地位",
        "对皇帝的政务能力持观望态度，期待更多关注",
        "感慨后宫生活不易，需要谨慎行事",
        "思念家人，希望有朝一日能重逢"
    ]

    # 妃子品级
    concubine_ranks = ["皇后", "皇贵妃", "贵妃", "德妃", "贤妃", "丽妃", "妃", "嫔", "贵人", "常在", "答应", "秀女"]
    # 皇嗣品级（男）
    male_heir_ranks = ["皇太子", "亲王", "郡王", "贝勒", "贝子", "镇国公"]
    # 皇嗣品级（女）
    female_heir_ranks = ["固伦公主", "和碩公主", "郡主", "县主", "郡君", "县君"]

    # 生成妃嫔（合并到characters）
    characters = []

    for i in range(concubine_count):
        characters.append({
            "name": names[i % len(names)],
            "type": "妃嫔",
            "gender": "女",
            "personality": random.choice(personalities),
            "intro": random.choice(concubine_intros),
            "mood": random.choice(moods),
            "thought": random.choice(thoughts),
            "mother": "无",
            "rank": concubine_ranks[min(i, len(concubine_ranks) - 1)],
            "favorability": random.randint(40, 70),
            "sincerity": random.randint(20, 50)
        })

    # 生成皇嗣（合并到characters）
    heir_male_names = ["弘历", "永琰", "永璜", "永璋"]
    heir_female_names = ["永康", "永宁", "永安", "和平"]
    heir_intros = [
        "聪慧过人的少年皇嗣，深得皇帝喜爱",
        "年幼的皇嗣，正在接受宫廷教育",
        "活泼可爱的孩童，对宫廷生活充满好奇",
        "沉稳内敛的少年皇嗣"
    ]
    heir_personalities = ["聪明伶俐", "活泼好动", "沉稳内敛", "调皮捣蛋", "勤奋好学"]
    heir_moods = ["开心", "认真", "好奇", "调皮"]
    heir_thoughts = [
        "希望能够得到父皇的更多关注",
        "努力学习，期待将来能够辅佐父皇",
        "对后宫的事情感到好奇",
        "想要快些长大，为父皇分忧"
    ]
    mother_options = ["无记载", "已故", "奴婢", names[0] if concubine_count > 0 else "无记载"]

    for i in range(heir_count):
        is_male = i % 2 == 0
        characters.append({
            "name": heir_male_names[i % len(heir_male_names)] if is_male else heir_female_names[i % len(heir_female_names)],
            "type": "皇嗣",
            "gender": "男" if is_male else "女",
            "intro": random.choice(heir_intros),
            "personality": random.choice(heir_personalities),
            "mood": random.choice(heir_moods),
            "thought": random.choice(heir_thoughts),
            "mother": random.choice(mother_options),
            "rank": male_heir_ranks[min(i, len(male_heir_ranks) - 1)] if is_male else female_heir_ranks[min(i, len(female_heir_ranks) - 1)],
            "favorability": random.randint(50, 80),
            "sincerity": random.randint(30, 60)
        })

    return {
        "emperor": {
            "name": emperor_name,
            "gender": gender,
            "background": background[:30],
            "talent": talents[0],
            "martial": talents[1],
            "appearance": talents[2],
            "morality": talents[3]
        },
        "characters": characters
    }

@app.route('/')
def index():
    """剧本选择界面"""
    return render_template('index.html')

@app.route('/create_character/<game_type>')
def create_character(game_type):
    """角色创建界面"""
    return render_template('create_character.html', game_type=game_type)

@app.route('/game/<game_type>')
def game(game_type):
    """游戏主界面"""
    if 'game_data' not in session:
        return render_template('index.html')
    return render_template('game.html', game_type=game_type)

@app.route('/api/init_game', methods=['POST'])
def init_game():
    """初始化游戏（任务队列版本）"""
    data = request.json

    emperor_name = data.get('emperor_name', '默认皇帝')
    gender = data.get('gender', '男')
    concubine_count = min(int(data.get('concubine_count', 1)), 5)
    heir_count = min(int(data.get('heir_count', 0)), 3)
    background = data.get('background', '世袭继承皇位')

    # 创建任务
    task_id = create_task()

    # 在后台线程中执行
    def run_init():
        try:
            game_data = generate_game_content(emperor_name, gender, concubine_count, heir_count, background)
            return game_data
        except Exception as e:
            print(f"API调用错误: {e}")
            return generate_fallback_data(emperor_name, gender, concubine_count, heir_count, background)

    run_task_in_background(task_id, run_init)

    # 立即返回任务ID
    return jsonify({
        "success": True,
        "task_id": task_id
    })

@app.route('/api/get_task_result/<task_id>', methods=['GET'])
def get_task_result(task_id):
    """获取任务结果（轮询接口）"""
    result = _get_task_result_internal(task_id)

    if result is None:
        return jsonify({
            "success": False,
            "error": "任务不存在"
        })

    if result["status"] == "done":
        return jsonify({
            "success": True,
            "status": "done",
            "result": result["result"]
        })
    elif result["status"] == "error":
        return jsonify({
            "success": False,
            "status": "error",
            "error": result["error"]
        })
    else:
        return jsonify({
            "success": True,
            "status": result["status"]
        })

@app.route('/api/get_game_data')
def get_game_data():
    """获取游戏数据"""
    if 'game_data' not in session:
        return jsonify({'success': False, 'error': '没有游戏数据'})
    return jsonify({'success': True, 'data': session['game_data']})

@app.route('/api/save_game_data', methods=['POST'])
def save_game_data():
    """保存游戏数据"""
    data = request.json
    game_data = data.get('game_data')
    if game_data:
        session['game_data'] = game_data
    return jsonify({'success': True})

# 剧情推动大模型系统提示词
STORY_SYSTEM_PROMPT = """你是一个专业的皇帝后宫模拟游戏的主持人（Story Master）。

## 游戏背景
你是皇帝后宫模拟器的主持人，负责推动剧情发展。你的任务是根据玩家的行动，生成沉浸式的剧情描述，并更新相关的属性变化。

## 当前游戏状态
皇帝信息：{emperor_info}
人物列表：{characters_info}

## 人物属性说明
### 人物类型(type)：妃嫔/皇嗣
### 妃嫔品级（从高到低）：正一品(皇后)、从一品(皇贵妃)、正二品(贵妃)、从二品(德妃)、正三品(贤妃)、从三品(丽妃)、正四品(妃)、从四品(嫔)、正五品(贵人)、从五品(常在)、正六品(答应)、从六品(秀女)
### 皇子宫品级（从高到低）：正一品(皇太子)、从一品(亲王)、正二品(郡王)、从二品(贝勒)、正三品(贝子)、从三品(镇国公)
### 公主品级（从高到低）：正一品(固伦公主)、从一品(和碩公主)、正二品(郡主)、从二品(县主)、正三品(郡君)、从三品(县君)
### 好感度：-100到100，代表对皇帝的好感程度，0以下为厌恶
### 真心度：-100到100，代表对皇帝的真心程度，0以下为虚情假意

## 剧情规则
1. 剧情要符合古代皇宫的场景和礼仪
2. 根据过往剧情分析皇帝性格，并根据性格和事件发展生成连贯合理的剧情
3. **每次剧情必须更新所有相关人物（妃嫔和皇嗣）的属性，不能遗漏任何一个相关人物！**
4. 皇帝的言行会影响自己的属性（才华、武力、容貌、道德），四项属性或其中几个要有合理的变化
5. 与人物（妃嫔/皇嗣）的互动会影响他们的心境、想法、好感度和真心度
6. 只有当玩家明确提到要册封、晋升、降职、废黜等时，才能更新品级(rank)，否则不要改变
7. 行动会影响多个人物，只有涉及事件的相关人物才会变化，不要一下变动所有人物（但即使是0变化也要返回）

## 输出格式要求（非常重要！）
输出分为两段，中间无任何分隔标题：

第一段：纯文字剧情，400~600字，沉浸式第二人称描述（称主角为你），不得出现任何JSON符号或格式标记。
第二段：紧接着输出以下JSON（用```json包裹），不要有任何额外说明：

```json
{{
    "attribute_changes": {{
        "emperor": {{"talent": 变化值, "martial": 变化值, "appearance": 变化值, "morality": 变化值}},
        "characters": [{{"name": "人物姓名", "type": "妃嫔/皇嗣", "mood": "新的心境", "thought": "新的想法（50字以内）", "favorability": 好感度变化值(-30到30), "sincerity": 真心度变化值(-30到30)}}]
    }},
    "next_suggestions": {{
        "gentle": "偏温柔角度的行动建议",
        "aggressive": "偏严肃角度的行动建议",
        "calm": "偏沉稳角度的行动建议",
        "random": "灵活推演，为玩家生成当前你认为最好的下一步行动"
    }},
    "new_character": {{}}
}}
```

**关键要求（必须遵守）：**
1. 皇帝的属性（talent, martial, appearance, morality）四项都必须返回变化值，即使是0也要写0
2. **必须返回当前游戏中的每一个人物的attribute_changes条目，不能遗漏！但并不是所有的都要有变化**
3. 只有涉及到的人物的心境(mood)和想法(thought)才需要更新
4. 好感度和真心度可以有变化（正负30以内），也可以设为0表示不变
5. rank字段只有玩家明确要求册封/晋升/贬黜时才添加到对应characters条目中
6. 想法（thought）必须是对当前局势和皇帝的最新看法，50字以内
7. **新人物(new_character)生成规则：**
   - 只有在剧情中自然遇到新人物时才填写，否则new_character保持为空对象{{}}
   - 新人物身份不能是已册封的妃子（妃子必须通过选秀或册封剧情出现）
   - 身份可以是：选秀入宫的女子、民间偶遇的人物、朝中大臣、宫女/太监、皇嗣、亲属等，一旦出现与剧情相关的现在没有的人物，你就要考虑是否应该加进来，不论男女老少。
   - 当前人物数量已达20人时，禁止生成新人物
   - 若有新人物，new_character格式为：{{"name":"姓名","type":"类型","gender":"男/女","intro":"简介","personality":"性格","mood":"心境","thought":"想法","rank":"品级或无","favorability":数值,"sincerity":数值}}
8. 第一段和第二段之间不要有任何额外的标题、说明或分隔符"""


@app.route('/api/get_suggestions', methods=['POST'])
def get_suggestions():
    """获取行动建议（基于剧情历史，任务队列版本）"""
    data = request.json
    game_data = data.get('game_data', {})
    history = data.get('history', [])

    emperor = game_data.get('emperor', {})
    characters = game_data.get('characters', [])

    emperor_info = f"姓名：{emperor.get('name', '未知')}，性别：{emperor.get('gender', '未知')}，才华：{emperor.get('talent', 0)}，武力：{emperor.get('martial', 0)}，容貌：{emperor.get('appearance', 0)}，道德：{emperor.get('morality', 0)}"

    characters_info = ", ".join([f"{c.get('name', '')}(类型：{c.get('type', '')}，{c.get('personality', '')}, {c.get('mood', '')})" for c in characters])

    # 构建历史上下文（精简为：1个原始对话 + 22个摘要）
    history_context = ""
    if history:
        # 分离原始对话和摘要
        original_dialogues = [h for h in history if h.get('role') in ['user', 'assistant']]
        summaries = [h for h in history if h.get('role') == 'summary']

        # 只取最近1条原始对话
        recent_original = original_dialogues[-2:] if len(original_dialogues) >= 2 else original_dialogues  # user + assistant
        # 只取最近22个摘要
        recent_summaries = summaries[-22:] if len(summaries) > 22 else summaries

        history_parts = []
        for h in recent_original:
            role_name = "玩家" if h.get('role') == 'user' else "剧情"
            history_parts.append(f"{role_name}: {h.get('content', '')}")
        for h in recent_summaries:
            history_parts.append(f"摘要: {h.get('content', '')}")

        if history_parts:
            history_context = "\n\n剧情发展历史：\n" + "\n".join(history_parts)

    system_prompt = """你是一个皇帝后宫模拟器的AI助手。根据当前游戏状态和剧情发展历史，给出4个不同角度的行动建议。

输出格式必须是JSON：
{
    "gentle": "温柔风格建议",
    "aggressive": "严肃风格建议",
    "calm": "沉稳风格建议",
    "random": "随机风格建议"
}

建议要求：
1. 必须基于剧情发展历史，确保建议符合当前局势和逻辑，是对上一步剧情发展的可能延伸，不能跳脱
2. 符合古代皇宫场景
3. 结合当前人物（妃嫔和皇嗣）的心境和与皇帝的关系，字数在20字以内
4. gentle：体贴关怀型，如对当前事件反应较为随和
5. aggressive：严肃型，如对当前事件反应较为严厉
6. calm：沉稳冷静型，如对当前事件考虑较为周全
7. random：随机推演型，给出你认为对当前事件最好的反应

直接输出JSON，不要其他文字。"""

    prompt = f"""皇帝信息：{emperor_info}
人物列表：{characters_info}
{history_context}

请根据以上信息，基于剧情发展历史生成4个不同角度的行动建议。"""

    # 创建任务
    task_id = create_task()

    def run_suggestions():
        try:
            result = call_deepseek(prompt, system_prompt)

            # 解析JSON
            try:
                start = result.find('{')
                end = result.rfind('}') + 1
                if start != -1 and end != 0:
                    suggestions = json.loads(result[start:end])
                    return {"success": True, "suggestions": suggestions}
            except:
                pass

            # 解析失败返回默认建议
            return {
                "success": True,
                "suggestions": {
                    'gentle': '召见妃子品茶谈心',
                    'aggressive': '选秀扩充后宫',
                    'calm': '在御花园散步思考',
                    'random': '微服出宫巡视'
                }
            }
        except Exception as e:
            print(f"获取建议失败: {e}")
            return {
                "success": True,
                "suggestions": {
                    'gentle': '召见妃子品茶谈心',
                    'aggressive': '选秀扩充后宫',
                    'calm': '在御花园散步思考',
                    'random': '微服出宫巡视'
                }
            }

    run_task_in_background(task_id, run_suggestions)

    # 立即返回任务ID
    return jsonify({
        "success": True,
        "task_id": task_id
    })


@app.route('/api/generate_summary', methods=['POST'])
def generate_summary():
    """生成对话摘要（任务队列版本）"""
    data = request.json
    action = data.get('action', '')
    story = data.get('story', '')

    if not action or not story:
        return jsonify({'success': False, 'error': '缺少必要参数'})

    system_prompt = """你是一个专业的剧情摘要助手。你的任务是将皇帝后宫模拟器的对话压缩成简短摘要。

输出格式必须是：
行动|剧情

要求：
1. 玩家行动：精确概括玩家做了什么，15字以内
2. 剧情：精确概括剧情结果（什么人什么事什么结果），30字以内
3. 用"|"分隔两部分
4. 直接输出，不要有任何解释"""

    prompt = f"""玩家行动：{action}
剧情发展：{story}

请按格式生成摘要："""

    # 创建任务
    task_id = create_task()

    def run_summary():
        try:
            result = call_deepseek(prompt, system_prompt)
            # 清理可能的markdown格式
            result = result.strip()
            if result.startswith('```'):
                result = result.split('\n', 1)[1]
            if result.endswith('```'):
                result = result.rsplit('```', 1)[0]
            result = result.strip()

            return {
                'success': True,
                'summary': result[:30]
            }
        except Exception as e:
            print(f"生成摘要失败: {e}")
            return {
                'success': True,
                'summary': ''
            }

    run_task_in_background(task_id, run_summary)

    # 立即返回任务ID
    return jsonify({
        "success": True,
        "task_id": task_id
    })


@app.route('/api/execute_action', methods=['POST'])
def execute_action():
    """执行玩家行动（SSE流式版本）"""
    data = request.json

    action = data.get('action', '')
    style = data.get('style', 'custom')
    game_data = data.get('game_data', {})
    history = data.get('history', [])

    emperor = game_data.get('emperor', {})
    characters = game_data.get('characters', [])

    # 构建皇帝和人物信息
    emperor_info = f"姓名：{emperor.get('name', '未知')}，性别：{emperor.get('gender', '未知')}，才华：{emperor.get('talent', 0)}，武力：{emperor.get('martial', 0)}，容貌：{emperor.get('appearance', 0)}，道德：{emperor.get('morality', 0)}"
    characters_info = ", ".join([f"{c.get('name', '')}(类型：{c.get('type', '')}，性别：{c.get('gender', '')}，品级：{c.get('rank', '')}，性格：{c.get('personality', '')}，心境：{c.get('mood', '')}，想法：{c.get('thought', '')}，好感度：{c.get('favorability', 0)}，真心度：{c.get('sincerity', 0)})" for c in characters])

    # 构建历史对话（记忆功能）
    history_context = ""
    if history:
        original_dialogues = [h for h in history if h.get('role') in ['user', 'assistant']]
        summaries = [h for h in history if h.get('role') == 'summary']
        recent_original = original_dialogues[-2:] if len(original_dialogues) >= 2 else original_dialogues
        recent_summaries = summaries[-22:] if len(summaries) > 22 else summaries

        history_parts = []
        for h in recent_original:
            role_name = "玩家" if h.get('role') == 'user' else "剧情"
            history_parts.append(f"{role_name}: {h.get('content', '')}")
        for h in recent_summaries:
            history_parts.append(f"{h.get('content', '')}")

        if history_parts:
            history_context = "\n\n剧情发展历史：\n" + "\n".join(history_parts)

    system_prompt = STORY_SYSTEM_PROMPT.format(
        emperor_info=emperor_info,
        characters_info=characters_info
    )

    user_message = f"""玩家行动：{action}
行动风格：{style}
{history_context}

请根据以上信息，生成剧情发展和属性变化。"""

    def generate():
        """SSE流式生成器"""
        story = ""
        story_sent_len = 0
        in_json_mode = False
        attribute_changes = {}
        suggestions = {}
        done_sent = False
        new_character = {}
        received_content = ""

        try:
            for chunk in call_deepseek_stream(user_message, system_prompt):
                received_content += chunk

                if not in_json_mode:
                    clean_content = re.sub(r'```json\s*', '', received_content)
                    clean_content = re.sub(r'```\s*$', '', clean_content)
                    json_start = clean_content.find('{')

                    if json_start == -1:
                        if len(clean_content) > story_sent_len:
                            new_text = clean_content[story_sent_len:]
                            story = clean_content
                            story_sent_len = len(clean_content)
                            # 发送故事片段
                            yield f"data: {json.dumps({'type': 'story', 'content': new_text})}\n\n"
                    else:
                        story_part = clean_content[:json_start]
                        if len(story_part) > story_sent_len:
                            new_text = story_part[story_sent_len:]
                            story = story_part.strip()
                            story_sent_len = len(story_part)
                            # 发送故事片段
                            if new_text.strip():
                                yield f"data: {json.dumps({'type': 'story', 'content': new_text})}\n\n"
                        in_json_mode = True

                if in_json_mode and not done_sent:
                    clean_buf = re.sub(r'```json\s*', '', received_content)
                    clean_buf = re.sub(r'```\s*', '', clean_buf)
                    j_start = clean_buf.find('{')
                    j_end = clean_buf.rfind('}') + 1

                    print(f"DEBUG: JSON模式检测 - j_start: {j_start}, j_end: {j_end}, done_sent: {done_sent}")

                    if j_start != -1 and j_end > j_start:
                        try:
                            parsed = json.loads(clean_buf[j_start:j_end])
                            attribute_changes = parsed.get('attribute_changes', {})
                            suggestions = parsed.get('next_suggestions', {})
                            new_character = parsed.get('new_character', {})

                            print(f"DEBUG: JSON解析成功, new_character: {new_character}")

                            # 发送完成事件
                            yield f"data: {json.dumps({'type': 'done', 'attribute_changes': attribute_changes, 'suggestions': suggestions, 'new_character': new_character if new_character and new_character.get('name') else {}})}\n\n"
                            done_sent = True
                            return
                        except json.JSONDecodeError as e:
                            print(f"DEBUG: JSON解析失败: {e}")

            # 未能完整解析，使用备用数据
            print(f"DEBUG: 循环结束, done_sent: {done_sent}")
            if not done_sent:
                fallback = execute_action_fallback_data(action, style, emperor, characters)
                yield f"data: {json.dumps({'type': 'done', 'attribute_changes': fallback['attribute_changes'], 'suggestions': fallback['suggestions'], 'new_character': {}, 'fallback': True})}\n\n"

        except Exception as e:
            print(f"SSE生成错误: {e}")
            fallback = execute_action_fallback_data(action, style, emperor, characters)
            yield f"data: {json.dumps({'type': 'done', 'attribute_changes': fallback['attribute_changes'], 'suggestions': fallback['suggestions'], 'new_character': {}, 'fallback': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no'
        }
    )


def execute_action_fallback_data(action, style, emperor, characters=None):
    """执行行动的备用数据（当API调用失败时）"""
    import random
    if characters is None:
        characters = []

    # 根据风格生成不同剧情
    story_templates = {
        '温柔': f"皇帝{action}，展现出了温柔体贴的一面。妃嫔和皇嗣们感受到圣恩，心生欢喜。",
        '激进': f"皇帝{action}，行事果断雷厉风行。后宫众人不敢有违，秩序井然。",
        '沉稳': f"皇帝{action}，举止沉稳从容。众人都在猜测圣意，不敢轻举妄动。",
        '随机': f"皇帝{action}，这一举动出乎众人意料。后宫一时间议论纷纷...",
        'custom': f"皇帝{action}，后宫之中悄然发生了变化..."
    }

    story = story_templates.get(style, story_templates['custom'])

    # 生成属性变化
    attribute_changes = {
        'emperor': {
            'talent': random.randint(-2, 2),
            'martial': random.randint(-2, 2),
            'appearance': random.randint(-2, 2),
            'morality': random.randint(-2, 2)
        },
        'characters': []
    }

    # 更新一个人物的心境和想法
    if characters:
        random_character = random.choice(characters)
        char_type = random_character.get('type', '妃嫔')
        if char_type == '妃嫔':
            moods = ["欣喜", "担忧", "感动", "期待", "忐忑", "安心"]
            thoughts = [
                "皇帝今日似乎心情不错",
                "希望能够得到更多关注",
                "后宫局势似乎有变化"
            ]
        else:
            moods = ["开心", "认真", "好奇", "调皮"]
            thoughts = [
                "父皇今天似乎很高兴",
                "想要得到父皇的夸奖",
                "学习不能懈怠"
            ]
        attribute_changes['characters'].append({
            'name': random_character.get('name', ''),
            'type': char_type,
            'mood': random.choice(moods),
            'thought': random.choice(thoughts),
            'favorability': random.randint(-10, 15),
            'sincerity': random.randint(-5, 10)
        })

    # 建议
    suggestions = {
        'gentle': '召见妃子品茶谈心',
        'aggressive': '选秀扩充后宫',
        'calm': '在御花园散步思考',
        'random': '微服出宫巡视'
    }

    return {
        'story': story,
        'attribute_changes': attribute_changes,
        'suggestions': suggestions
    }


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)