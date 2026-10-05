# 钻孔地层关联校核台

基于 **Python Flask + SQLite + 原生 HTML/CSS/JavaScript** 的单容器 Web 工具，
用于多口钻孔岩性层段的井间自动关联与人工校核。

## 一条命令启动

```bash
docker compose up -d --build
```

浏览器访问 <http://localhost:8000> 即可（内置 4 口演示钻孔 ZK01–ZK04 与 2 个标志层 M1/L1；
ZK04 在 M1–L1 之间含一对高程对称的分叉砂岩层，默认参数下即为“并列最优 + 歧义区间”的天然示例）。

停止：`docker compose down`；数据持久化在宿主机 `./data/correlation.db`。

### 不用 Docker（本地开发）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python wsgi.py          # http://localhost:8000
```

### 运行测试

核心算法（`app/correlator.py`）不依赖 Flask，可直接运行回归测试
（含“全零参数下单层对单层只计 2 个并列解”、“低开间隙惩罚 `gap_open=0`、
`gap_extend=1` 下 2×2 应得 6 个归属、不漏全部未匹配”、“极大开间隙惩罚
`gap_open=1e20` 下合法负分 `-1e20` 不被当作不可达、2×1 应得 2 个归属”与
“大数值 `gap_open=1e20`、`gap_extend=9.999999999e19` 下 3×1 只算首/末层
两个归属、中层不伪并列”四类反例，以及大数值真并列保留、容差边界和多
标志层分段锚点组合用例；另含两个端到端 HTTP 用例，无 Flask 时自动跳过）：

```bash
python3 -m unittest discover -s tests -v
```

### 复现“极大间隙惩罚把合法负分误判为不可达”反例

`PUT /api/settings` 允许任意**非负** `gap_open` / `gap_extend` 组合，取值
可以任意大（如 `1e20`）。当五项相似度权重全为 0、参考孔两层、待校核孔
一层且不锁标志层时，待校核层无论匹配参考孔第一层还是第二层，都恰好有一个
参考层未匹配（开一个间隙游程），最优分值是合法的负数 `-1e20`。旧实现以
`-1e18` 作“不可达”哨兵，`-1e20` 比哨兵更负，可达状态被当成不可达、DP 表
被幻影值污染，`POST /api/correlate` 在规范回溯时直接报 500。修复后哨兵
为真正的负无穷、最优性比较按分值尺度缩放，下面的独立数据调用应得到
**`dp_score=-1e20`、两个并列归属（各留一个未匹配参考层）**，
`solution_count` 与 `solutions_listed` 均为 2，不再返回 500：

```bash
docker compose up -d --build
BASE=http://localhost:8000

# 独立数据：参考孔两层、待校核孔一层（属性相同）
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"R","collar_elev":100,"x":0,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9},
    {"top":2,"bottom":4,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"H","collar_elev":100,"x":10,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null

# 五项权重全 0、极大开间隙惩罚
curl -s -XPUT $BASE/api/settings -H 'Content-Type: application/json' -d '{
  "weights":{"lithology":0,"grain":0,"water":0,"elev":0,"confidence":0},
  "gap_open":1e20,"gap_extend":1}' >/dev/null

RID=$(curl -s $BASE/api/state | python3 -c 'import sys,json;print(next(b["id"] for b in json.load(sys.stdin)["boreholes"] if b["name"]=="R"))')
# 不锁标志层：marker_ids 留空
curl -s -XPOST $BASE/api/correlate -H 'Content-Type: application/json' \
  -d "{\"reference_id\":$RID,\"marker_ids\":[]}" \
| python3 -c 'import sys,json;h=json.load(sys.stdin)["holes"][0];print("dp_score =",h["dp_score"],"| solution_count =",h["solution_count"],"| solutions_listed =",h["solutions_listed"]);[print(s["pairs"],"unref",s["unmatched_ref"],"unhole",s["unmatched_hole"]) for s in h["tie_solutions"]]'
# 期望：
#   dp_score = -1e+20 | solution_count = 2 | solutions_listed = 2
#   [(<R1 的 id>, <H1 的 id>)] unref [<R2 的 id>] unhole []
#   [(<R2 的 id>, <H1 的 id>)] unref [<R1 的 id>] unhole []
# 「导出 JSON」(GET /api/export) 的 last_result 与重算结果口径一致。
```

### 复现“大数值间隙费用下的伪并列”反例

`PUT /api/settings` 允许任意**非负** `gap_open` / `gap_extend`，取值可以
任意大。当五项相似度权重全为 0、参考孔**三层**、待校核孔**一层**、不锁
标志层，并设 `gap_open=1e20`、`gap_extend=9.999999999e19` 时：待校核层
匹配参考孔**首层或末层**都只开一个间隙游程（代价
`-(open+extend)=-1.9999999999e20`），是真正的并列最优；而匹配**中层**要在
匹配前后各开一次间隙（两个游程，代价 `-2e20`），真实分差 `1e10`（该量级
约 30 万个浮点 ULP），严格次优。旧实现在最优性比较中使用与分值成比例的
相对容差 `1e-9`，其窗口在 2e20 量级被放大到 `2e11`，反而把中层的低分归属
算进并列，`solution_count` / `solutions_listed` 都错误地变成 3，中层还混入
主解与歧义区间。修复后容差改为 ULP 口径（`1e20` 量级一个 ULP 约 1.6e4，
余量 4096 ULP ≈ 1.3e8，远小于 1e10 的真实分差），独立数据调用应得到
**`dp_score=-1.9999999999e20`、恰好两个并列归属（首层 / 末层）**，中层只出
现在未匹配侧，歧义区间只覆盖首、末两层：

```bash
docker compose up -d --build
BASE=http://localhost:8000

# 独立数据：参考孔三层、待校核孔一层（属性相同）
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"R","collar_elev":100,"x":0,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9},
    {"top":2,"bottom":4,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9},
    {"top":4,"bottom":6,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"H","collar_elev":100,"x":10,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null

# 五项权重全 0；open 与 extend 都取 1e20 量级但相差 1
curl -s -XPUT $BASE/api/settings -H 'Content-Type: application/json' -d '{
  "weights":{"lithology":0,"grain":0,"water":0,"elev":0,"confidence":0},
  "gap_open":1e20,"gap_extend":9.999999999e19}' >/dev/null

RID=$(curl -s $BASE/api/state | python3 -c 'import sys,json;print(next(b["id"] for b in json.load(sys.stdin)["boreholes"] if b["name"]=="R"))')
# 不锁标志层：marker_ids 留空
curl -s -XPOST $BASE/api/correlate -H 'Content-Type: application/json' \
  -d "{\"reference_id\":$RID,\"marker_ids\":[]}" \
| python3 -c 'import sys,json;h=json.load(sys.stdin)["holes"][0];print("dp_score =",h["dp_score"],"| solution_count =",h["solution_count"],"| solutions_listed =",h["solutions_listed"]);[print(s["pairs"],"unref",s["unmatched_ref"],"unhole",s["unmatched_hole"]) for s in h["tie_solutions"]];print("歧义参考层序号 =",[a["ref_seq"] for a in h["ambiguities"]])'
# 期望：
#   dp_score = -1.9999999999e+20 | solution_count = 2 | solutions_listed = 2
#   [(<R1>, <H1>)] unref [<R2>, <R3>] unhole []
#   [(<R3>, <H1>)] unref [<R1>, <R2>] unhole []
#   歧义参考层序号 = [1, 3]      # 中层 R2 在任何最优路径上都只能未匹配
# 「导出 JSON」(GET /api/export) 的 last_result 与上述口径一致。
```

同量级下**逐位相等的真正并列不受影响**：把上例改成参考孔两层、待校核孔
一层并设 `gap_open=gap_extend=1e20`，匹配首层与匹配末层的代价同为
`-1e20`，仍返回两个并列归属（见测试 `HugeGapPenaltyTest` 中的
`test_genuine_tie_preserved_at_huge_scale` /
`test_genuine_tie_preserved_huge_open_small_extend`）。

### 复现“低开间隙惩罚漏解”反例

`PUT /api/settings` 允许任意**非负** `gap_open` / `gap_extend` 组合，包括
`gap_open < gap_extend`（如 0 / 1）。当五项相似度权重全为 0、两口孔各有两层
时，“全部未匹配”归属可让两侧跳步交错、每步都重开免费间隙（分值 0）；若把
“先跳完一侧再跳另一侧”的顺序写进计分 DP，就要承担续间隙惩罚，导致该归属被
漏判。下面用可执行调用复现修复后应得到 **6 个不同归属**（全部未匹配只计、
只列一次）：

```bash
docker compose up -d --build
BASE=http://localhost:8000

# 两口各两层、属性完全相同的孔
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"R","collar_elev":100,"x":0,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9},
    {"top":2,"bottom":4,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null
curl -s -XPOST $BASE/api/boreholes -H 'Content-Type: application/json' -d '{
  "name":"H","collar_elev":100,"x":10,"layers":[
    {"top":0,"bottom":2,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9},
    {"top":2,"bottom":4,"lithology":"砂岩","grain":"中粒","water":"稍湿","confidence":0.9}]}' >/dev/null

# 五项权重全 0、低开间隙 / 高续间隙
curl -s -XPUT $BASE/api/settings -H 'Content-Type: application/json' -d '{
  "weights":{"lithology":0,"grain":0,"water":0,"elev":0,"confidence":0},
  "gap_open":0,"gap_extend":1}' >/dev/null

RID=$(curl -s $BASE/api/state | python3 -c 'import sys,json;print(next(b["id"] for b in json.load(sys.stdin)["boreholes"] if b["name"]=="R"))')
curl -s -XPOST $BASE/api/correlate -H 'Content-Type: application/json' \
  -d "{\"reference_id\":$RID,\"marker_ids\":[]}" \
| python3 -c 'import sys,json;h=json.load(sys.stdin)["holes"][0];print("solution_count =",h["solution_count"]);[print(s["pairs"],"unref",s["unmatched_ref"],"unhole",s["unmatched_hole"]) for s in h["tie_solutions"]]'
# 期望：solution_count = 6，含一个 pairs=[]、unref=[1,2]、unhole=[1,2] 的“全部未匹配”归属
```

## 功能

- **数据录入**：多口钻孔的孔口高程、水平位置（剖面坐标），以及按深度排列的岩性层段
  （岩性、粒径、含水状态、置信度 0–1），深度重叠/非法值在服务端校验。
- **深度→高程统一**：层段坐标换算为 `孔口高程 − 埋深`，全部对比与剖面绘制均在高程域进行。
- **参考孔与标志层锁定**：任选一口孔作参考；标志层按孔指定对应层，锁定后成为强制对齐锚点，
  DP 匹配路径**禁止跨越锁定标志层**（锚点交叉顺序冲突会直接报错）。
- **带间隙惩罚的动态规划**：
  - 层间相似度 = 岩性 / 粒径 / 含水状态 / 高程接近度（指数衰减）/ 置信度五项加权；
  - 三态 DP（匹配态、参考孔间隙态、待校核孔间隙态）实现**仿射间隙代价**（开间隙 `gap_open`、续间隙 `gap_extend`），
    保持层序单调、允许尖灭/缺失/新增层；不可达哨兵为真正的负无穷，合法的负最优分
    （如 `gap_open=1e20` 下的 `-1e20`）不会被误判为不可达；最优性比较采用
    ULP 尺度容差（小分值绝对容差 `1e-7` 兜底、大分值 4096 个浮点 ULP），
    只吸收正/反表独立递推的舍入噪声，不会把“多开一次间隙”造成的 `1e10`
    级真实分差误判为并列；
  - 总分按 `匹配相似度之和 / max(两孔层数) × 100` 归一化（`dp_score` 为未归一化的 DP 原始分）。
- **输出**：相关层带（匹配对及五项相似度明细）、未匹配层（参考孔侧/待校核孔侧分列）、
  锚点对、总分与多孔平均分。
- **并列解与歧义区间**：正/反 DP 表联合判定每条边是否出现在某条最优路径上，
  - 列出存在并列最优的层位及其全部候选（含“可未匹配”结局）；
  - 给出候选的**高程歧义区间**（最低–最高高程、跨度），剖面图以紫色虚线候选连线和歧义带显示；
  - 并列解按“**匹配对集合 + 两侧未匹配层集合**”的等价关系计数：同一归属方案不会因
    间隙操作先后顺序（先跳过参考层还是先跳过待校核层）被重复计数，也不会因某种排列
    多付续间隙惩罚而漏判——最优分值由不受限 Gotoh DP（允许两侧间隙游程交错）计算，
    每个归属以其**可达最高 DP 分**参与比较；计数与枚举在“匹配节点 + 间隙区”上按归属
    进行，每个归属唯一对应一条规范步骤，多标志层分段组合再按同一签名兜底去重，
    `solution_count` 与 `solutions_listed` 口径一致（支持 `gap_open < gap_extend`、
    惩罚取值任意大等接口允许的全部非负惩罚组合）；
  - 并列解计数封顶 9999，并列方案枚举最多列出前 100 条，可在结果区弹窗逐方案查看。
- **剖面图**：SVG 绘制统一高程的钻孔柱状，连线颜色区分普通匹配 / 标志层锚点 / 歧义候选，
  未匹配层标红叉，悬停层块查看属性，可开关歧义带与相似度标注。
- **重算与导出**：随时调整五项权重、高程特征尺度和间隙惩罚后重算；
  「导出 JSON」下载包含钻孔、层段、标志层、当前参数和最近一次完整计算结果的归档文件。

## 岩性/属性相似度规则（可在 `app/domain.py` 调整）

| 属性 | 规则 |
|------|------|
| 岩性 | 完全相同 1.0；同岩性族 0.5；近缘族（如砂↔砾、泥岩↔页岩）0.2–0.3；未知 0.4 |
| 粒径 | 泥/细粒/中粒/粗粒/巨粒按档极差线性折减 |
| 含水状态 | 干燥→承压共 5 档，按档极差线性折减 |
| 高程接近度 | `exp(−|层中高程差| / elev_scale)`，默认特征尺度 5 m |
| 置信度 | 两孔该层置信度均值 |

默认权重：岩性 0.45、粒径 0.15、含水 0.10、高程 0.20、置信度 0.10（自动归一）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/state` | 钻孔/层段/标志层/参数/最近结果 |
| GET | `/api/config` | 岩性、粒径、含水状态字典与默认参数 |
| POST/PUT/DELETE | `/api/boreholes[/:id]` | 钻孔与层段 CRUD（层段随孔整体保存） |
| POST/PUT/DELETE | `/api/markers[/:id]` | 标志层及各孔层链接 CRUD |
| PUT | `/api/settings` | 权重、高程尺度、间隙惩罚 |
| POST | `/api/correlate` | `{reference_id, marker_ids}` 触发关联计算 |
| GET | `/api/export` | 下载 JSON 归档 |
| POST | `/api/demo/reset` | 恢复内置演示数据 |

## 目录结构

```
app/
  db.py           SQLite 建表 / 默认参数
  domain.py       岩性族字典、属性相似度
  correlator.py   三态 DP、标志层分段、正反表歧义判定、并列解枚举
  seed.py         内置演示数据
  routes.py       Flask 路由
  static/         index.html / style.css / app.js（原生前端，SVG 剖面）
tests/            并列解计数口径回归测试（unittest，无需 Flask）
wsgi.py           开发入口 & gunicorn 入口
Dockerfile / docker-compose.yml
```

## 计算结果 JSON 摘要

`POST /api/correlate` 返回结构（单孔部分）：

```json
{
  "hole_name": "ZK02",
  "total_score": 82.04,
  "pairs": [{ "ref_seq": 4, "hole_seq": 5, "score": 0.83,
              "detail": {"lithology": 1.0, "grain": 0.75, "water": 0.5,
                         "elev": 0.9, "confidence": 0.7},
              "marker": false }],
  "anchors":  [{ "ref_seq": 6, "hole_seq": 7, "score": 0.97 }],
  "unmatched_ref": [],
  "unmatched_hole": [{ "seq": 4, "lithology": "粗砂", "zmid": 105.75 }],
  "ambiguities": [{ "ref_seq": 10, "outcome_count": 2,
                    "partners": [{"seq": 10}, {"seq": 11}],
                    "can_be_unmatched": true,
                    "elev_min": 61.2, "elev_max": 64.8, "elev_span": 3.6 }],
  "solution_count": 1,
  "solutions_listed": 1,
  "tie_solutions": [ /* 并列方案：匹配对 id、未匹配层 id、各自总分 */ ]
}
```
