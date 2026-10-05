"""并列解计数口径回归测试。

修复背景：五项相似度权重与 gap_open/gap_extend 全为 0、参考孔与待校核孔
各只有一层时，“两层均匹配 / 两层均未匹配”两个归属方案曾被 H/V 间隙操作
顺序（先跳过参考层 vs 先跳过待校核层）重复计数为 3。修复后 solution_count
与 solutions_listed 统一按“匹配对集合 + 两侧未匹配层集合”的等价关系
去重：段内 DP 只走规范形路径（同一间隙段内参考侧间隙在待校核侧间隙之前），
多标志层分段组合按同一签名兜底去重；计数仍封顶 9999、最多列出前 100 条。

另一个反例（极大开间隙惩罚）：gap_open=1e20、gap_extend=1 时合法最优分值
本身约为 -1e20，旧实现用 -1e18 作“不可达”哨兵，合法负分被当作不可达，
正/反表被幻影值污染，POST /api/correlate 在规范回溯时 500。修复后哨兵
为真正的负无穷、最优性比较按分值尺度缩放，详见 HugeGapPenaltyTest。

第三个反例（大数值间隙费下的伪并列）：gap_open=1e20、
gap_extend=9.999999999e19、参考孔三层、待校核孔一层时，匹配端层只开
一次间隙（-1.9999999999e20），匹配中层要开两次（-2e20，相差 1e10、约
305 个 ULP）。旧的固定 1e-9 相对容差在 2e20 量级放大约 2e11，把严格
次优的中层归属误并入并列（solution_count=3）。修复后容差按“分值处
ULP × 路径长度”缩放，详见 PseudoTieHugeGapTest。

运行：python -m unittest discover -s tests -v
"""
import unittest

from app.correlator import correlate, prepare_layers
from app.db import DEFAULT_SETTINGS
from app.seed import BOREHOLES, LAYERS, MARKERS

ZERO_WEIGHTS = {"lithology": 0.0, "grain": 0.0, "water": 0.0,
                "elev": 0.0, "confidence": 0.0}


def make_layers(specs, collar=100.0):
    """specs: [(layer_id, lithology), ...]，深度按 2m 规则递增。"""
    raw = []
    for seq, (lid, lith) in enumerate(specs, 1):
        raw.append({
            "id": lid, "seq": seq,
            "top": (seq - 1) * 2.0, "bottom": seq * 2.0,
            "lithology": lith, "grain": "中粒", "water": "稍湿",
            "confidence": 0.9,
        })
    return prepare_layers(raw, collar)


def signatures(res):
    """每个列出方案的等价签名：匹配对集合 + 两侧未匹配层集合。"""
    return {
        (tuple(sorted(s["pairs"])),
         tuple(sorted(s["unmatched_ref"])),
         tuple(sorted(s["unmatched_hole"])))
        for s in res["tie_solutions"]
    }


class TieCountEquivalenceTest(unittest.TestCase):
    """solution_count 与 solutions_listed 使用同一等价关系。"""

    def test_single_layer_all_zero_counts_two_solutions(self):
        """任务反例：各一层、全零参数 -> 2 个方案而非 3。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), ()),
            ((), (101,), (201,)),
        })

    def test_optimal_subgraph_order_dedup(self):
        """2x2 全零：单调匹配集合共 6 个（1 空 + 4 单对 + 1 双对）。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        self.assertEqual(len(signatures(res)), 6)

    def test_multi_segment_combination_dedup(self):
        """多标志层分段组合：3 层对 3 层、中间层锁定 -> 两段 1x1 -> 4 个方案。"""
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 4)
        self.assertEqual(res["solutions_listed"], 4)
        # 锁定锚点强制出现在每个方案中
        for sol in res["tie_solutions"]:
            self.assertIn((102, 202), sol["pairs"])
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202), (103, 203)), (), ()),
            (((101, 201), (102, 202)), (103,), (203,)),
            (((102, 202), (103, 203)), (101,), (201,)),
            (((102, 202),), (101, 103), (201, 203)),
        })
        # 两个非锚点参考层各自给出“可未匹配”的歧义区间
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 2)

    def test_count_cap_and_listing_limit(self):
        """10x10 全零：C(20,10)=184756 个匹配集合 -> 计数封顶 9999、列出 100。"""
        ref = make_layers([(100 + i, "砂岩") for i in range(1, 11)])
        hole = make_layers([(200 + i, "砂岩") for i in range(1, 11)])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 9999)
        self.assertEqual(res["solutions_listed"], 100)
        self.assertEqual(len(res["tie_solutions"]), 100)
        self.assertEqual(len(signatures(res)), 100)


class DefaultParamsRegressionTest(unittest.TestCase):
    """默认参数下的既有行为（锚点、歧义区间、总分）保持不变。"""

    def _seed_holes(self):
        holes = {}
        for name, _x, elev, _note in BOREHOLES:
            raw = [{"id": f"{name}-{i}", "seq": i, "top": t, "bottom": b,
                    "lithology": li, "grain": g, "water": w, "confidence": c}
                   for i, (t, b, li, g, w, c) in enumerate(LAYERS[name], 1)]
            holes[name] = prepare_layers(raw, elev)
        return holes

    def test_seed_natural_tie_preserved(self):
        """演示数据 ZK04 分叉砂岩层的天然并列：2 个方案、1 处歧义区间。"""
        holes = self._seed_holes()
        s = DEFAULT_SETTINGS
        anchors = sorted((mp["ZK01"] - 1, mp["ZK04"] - 1)
                         for _lbl, _desc, mp in MARKERS)
        res = correlate(holes["ZK01"], holes["ZK04"], anchors,
                        s["weights"], s["elev_scale"],
                        s["gap_open"], s["gap_extend"])
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(len(res["ambiguities"]), 1)
        # 两个锁定锚点强制匹配（M1: 6<->6，L1: 8<->9）
        anchor_pairs = {(a["ref_seq"], a["hole_seq"]) for a in res["anchors"]}
        self.assertEqual(anchor_pairs, {(6, 6), (8, 9)})
        matched = {(p["ref_seq"], p["hole_seq"]) for p in res["pairs"]}
        self.assertTrue(anchor_pairs <= matched)
        self.assertGreater(res["total_score"], 0)

    def test_unique_solution_when_signal_clear(self):
        """强信号下只有 1 个最优方案，计数与列出一致。"""
        ref = make_layers([(101, "砂岩"), (102, "泥岩")])
        hole = make_layers([(201, "砂岩"), (202, "泥岩")])
        res = correlate(ref, hole, [], DEFAULT_SETTINGS["weights"],
                        5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["solutions_listed"], 1)
        self.assertEqual([(p["ref_layer_id"], p["hole_layer_id"])
                          for p in res["pairs"]], [(101, 201), (102, 202)])
        # 单项相似度：岩性/粒径/含水/高程 = 1.0，置信度 = 0.9 -> 0.99
        self.assertAlmostEqual(res["total_score"], 99.0)


class LowOpenPenaltyMissingAssignmentTest(unittest.TestCase):
    """gap_open < gap_extend 时，归属按其可达最高 DP 分参与比较。

    触发例：五项权重全为 0、两孔各两层、gap_open=0、gap_extend=1。
    “全部未匹配”归属交错跳过两侧层段时每步都重开免费间隙（分值 0），
    而固定先跳完一侧再跳另一侧要付续间隙惩罚（-2）。旧的计分 DP 强制
    后者规范形，导致只返回 5 个归属、漏掉“全部未匹配”。
    """

    EXPECTED_SIX = {
        (((101, 201), (102, 202)), (), ()),
        (((101, 201),), (102,), (202,)),
        (((102, 202),), (101,), (201,)),
        (((101, 202),), (102,), (201,)),
        (((102, 201),), (101,), (202,)),
        ((), (101, 102), (201, 202)),       # 全部未匹配：曾被漏解
    }

    def test_2x2_open0_extend1_returns_six_assignments(self):
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)
        self.assertEqual(len(res["tie_solutions"]), 6)
        self.assertEqual(signatures(res), self.EXPECTED_SIX)
        # 全部未匹配只计、只列一次
        all_gap = [s for s in res["tie_solutions"]
                   if not s["pairs"]
                   and sorted(s["unmatched_ref"]) == [101, 102]
                   and sorted(s["unmatched_hole"]) == [201, 202]]
        self.assertEqual(len(all_gap), 1)
        # 六个归属最优分值同为 0
        self.assertEqual({s["score"] for s in res["tie_solutions"]}, {0.0})
        self.assertEqual(res["dp_score"], 0.0)
        # 正反向判定：两层各自的歧义区间均含 3 个结局（2 个候选 + 未匹配）
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 3)
            self.assertEqual(len(amb["partners"]), 2)
        # 主解仍为对角双匹配（匹配优先），未匹配列表为空
        self.assertEqual([(p["ref_layer_id"], p["hole_layer_id"])
                          for p in res["pairs"]], [(101, 201), (102, 202)])
        self.assertEqual(res["unmatched_ref"], [])
        self.assertEqual(res["unmatched_hole"], [])

    def test_open0_extend1_single_layer_still_two(self):
        """1x1、open=0/extend=1：匹配 0 分；未匹配开 1 个免费间隙也 0 分 -> 2。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), ()),
            ((), (101,), (201,)),
        })

    def test_3x2_open0_extend1_all_unmatched_optimal(self):
        """3 层对 2 层、open=0/extend=1：全部未匹配交错 4 游程分值 0。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        # 全部匹配不可能（3!=2 单调），但任一含 0 分相似度匹配的归属
        # 剩余层可交错开免费间隙，最优总分仍为 0
        self.assertEqual(res["dp_score"], 0.0)
        sig = signatures(res)
        self.assertIn(((), (101, 102, 103), (201, 202)), sig)
        # 计数与列出一致、无重复签名
        self.assertEqual(res["solution_count"], res["solutions_listed"])
        self.assertEqual(len(sig), res["solutions_listed"])

    def test_anchor_segments_open0_extend1(self):
        """锁定标志层分段：3 层对 3 层、中层锁定 -> 两段 1x1 -> 4 个归属。

        每个 1x1 段“匹配/未匹配”在 open=0/extend=1 下同为 0 分；锚点强制
        出现在每个方案中，全部未匹配（指两个非锚点层均未匹配）恰好一个。
        """
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0, 0.0, 1.0)
        self.assertEqual(res["solution_count"], 4)
        self.assertEqual(res["solutions_listed"], 4)
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202), (103, 203)), (), ()),
            (((101, 201), (102, 202)), (103,), (203,)),
            (((102, 202), (103, 203)), (101,), (201,)),
            (((102, 202),), (101, 103), (201, 203)),
        })
        for sol in res["tie_solutions"]:
            self.assertIn((102, 202), sol["pairs"])
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 2)

    def test_open_ge_extend_unchanged_behavior(self):
        """正惩罚下匹配严格占优；全零惩罚退化时 6 个归属并列。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        # open=2、extend=1：任一未匹配层都付正代价，仅全匹配（0 惩罚）最优
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(
            [(p["ref_layer_id"], p["hole_layer_id"]) for p in res["pairs"]],
            [(101, 201), (102, 202)])
        # open=extend=1：未匹配仍付正代价，仅全匹配最优
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        # 开/续惩罚全为 0：所有归属代价同为 0，6 个并列
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)
        self.assertEqual(res["solutions_listed"], 6)


class HugeGapPenaltyTest(unittest.TestCase):
    """极大开间隙惩罚下，合法的负最优分不能被当作不可达。

    任务反例：参考孔两层、待校核孔一层、五项权重全 0，
    gap_open=1e20、gap_extend=1、不锁标志层。两种最优归属：
    待校核层匹配参考孔第一层（参考孔第二层未匹配）或匹配第二层
    （第一层未匹配），各只开一个间隙游程，分值同为 -1e20。
    旧实现哨兵 -1e18 高于合法分值 -1e20，导致 /api/correlate 500。
    """

    def test_2x1_huge_open_two_solutions_dp_neg(self):
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        # 合法负分：每个最优归属恰好开一个间隙游程
        self.assertEqual(res["dp_score"], -1e20)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102,), ()),       # 待校核层匹配参考第一层
            (((102, 201),), (101,), ()),       # 待校核层匹配参考第二层
        })
        # 两归属的归一化展示分都为 0（匹配相似度为 0），主解为其中之一
        self.assertEqual({s["score"] for s in res["tie_solutions"]}, {0.0})
        primary = {(p["ref_layer_id"], p["hole_layer_id"])
                   for p in res["pairs"]}
        self.assertIn(primary, [{(101, 201)}, {(102, 201)}])
        self.assertEqual(len(res["unmatched_hole"]), 0)
        self.assertEqual(len(res["unmatched_ref"]), 1)
        # 两个参考层均有歧义：匹配唯一待校核层 或 未匹配
        self.assertEqual(len(res["ambiguities"]), 2)
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])
            self.assertEqual(amb["outcome_count"], 2)
            self.assertEqual(len(amb["partners"]), 1)

    def test_1x1_huge_open_match_strictly_optimal(self):
        """1x1：匹配 0 分严格优于未匹配 -1e20，只有一个归属。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], 0.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["solutions_listed"], 1)
        self.assertEqual(signatures(res), {(((101, 201),), (), ())})

    def test_3x1_huge_open_only_endpoint_matches_optimal(self):
        """3 层对 1 层：匹配端层只开一个游程（-1e20-1）；匹配中层要开两个
        游程（-2e20），不是最优 -> 恰好两个归属。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], -1e20 - 1.0)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103), ()),
            (((103, 201),), (101, 102), ()),
        })

    def test_1x2_huge_open_two_solutions(self):
        """1 层对 2 层：参考层匹配任一待校核层，各留一个未匹配 -> 2 个。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], -1e20)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), (202,)),
            (((101, 202),), (), (201,)),
        })

    def test_2x2_huge_open_full_match_unique(self):
        """2x2：任何未匹配都付 -1e20 量级代价，仅全匹配（0 分）最优。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], 0.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(signatures(res),
                         {(((101, 201), (102, 202)), (), ())})

    def test_anchor_forced_gap_with_huge_open(self):
        """锁定标志层强制段内纯间隙：2 参考层、1 待校核层，锚点
        (第 2 参考层, 唯一待校核层)，上方段参考层必须未匹配 -> -1e20
        的合法负分，仍恰有一个归属且不报错。"""
        ref = make_layers([(101, "砂岩"), (102, "煤层")])
        hole = make_layers([(201, "煤层")])
        res = correlate(ref, hole, [(1, 0)], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], -1e20)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["solutions_listed"], 1)
        self.assertEqual([(p["ref_seq"], p["hole_seq"])
                          for p in res["anchors"]], [(2, 1)])
        self.assertEqual([u["seq"] for u in res["unmatched_ref"]], [1])
        self.assertEqual(res["unmatched_hole"], [])

    def test_huge_open_and_extend_equal(self):
        """open=extend=1e20 时 2x1 仍为两个合法归属（各开一个游程）。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1e20)
        self.assertEqual(res["dp_score"], -1e20)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102,), ()),
            (((102, 201),), (101,), ()),
        })

    def test_result_is_json_finite(self):
        """极大惩罚下结果结构内不含 Infinity/NaN（导出 JSON 必须合法）。"""
        import json
        import math
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        text = json.dumps(res)
        self.assertNotIn("Infinity", text)
        self.assertNotIn("NaN", text)
        self.assertTrue(math.isfinite(res["dp_score"]))


class PseudoTieHugeGapTest(unittest.TestCase):
    """大数值间隙费下，差一个完整间隙代价的严格次优归属不得伪并列。

    任务反例：参考孔三层、待校核孔一层，五项权重全 0，不锁标志层，
    gap_open=1e20、gap_extend=9.999999999e19。待校核层匹配参考孔首层
    或末层都只开一次间隙（一个游程 = open + extend），最优分值同为
    -1.9999999999e20；匹配中层须在两侧各开一次间隙（两个游程），分值
    -2e20，严格次优 1e10（2e20 处约 305 个 ULP）。旧实现的固定 1e-9
    相对容差在该量级放大约 2e11，中层被误判并列：计数与列出均为 3、
    中层还污染了正反向歧义判定。修复后容差按“分值处 ULP × 路径长度”
    缩放（一条 n+m 的仿射间隙路径至多累积 n+m 个 ULP 舍入），中层不得
    进入主解、tie_solutions 或歧义区间，计数与列出均为 2。
    """

    GO = 1e20
    GE = 9.999999999e19
    END_SCORE = -1.9999999999e20

    def test_3x1_large_extend_only_endpoints_optimal(self):
        """任务反例主用例：3 层对 1 层，中层严格次优 -> 恰好两个归属。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, self.GO, self.GE)
        # 最优分值：一个游程 open+extend；两次游程为 -2e20，严格更差
        self.assertEqual(res["dp_score"], self.END_SCORE)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103), ()),      # 匹配首层
            (((103, 201),), (101, 102), ()),      # 匹配末层
        })
        # 中层不进入任何并列方案
        for sol in res["tie_solutions"]:
            self.assertNotIn((102, 201), sol["pairs"])
            self.assertIn(102, sol["unmatched_ref"])
        # 主解匹配首层或末层，且恰有两个未匹配参考层
        primary = {(p["ref_layer_id"], p["hole_layer_id"])
                   for p in res["pairs"]}
        self.assertIn(primary, [{(101, 201)}, {(103, 201)}])
        self.assertEqual(len(res["unmatched_ref"]), 2)
        self.assertEqual(res["unmatched_hole"], [])
        # 歧义区间只涉及首层、末层；中层在所有最优路径上恒为“未匹配”，
        # 不产生歧义（不出现在 partners，也不算多结局）
        amb = {a["ref_seq"]: a for a in res["ambiguities"]}
        self.assertEqual(set(amb), {1, 3})
        for seq in (1, 3):
            self.assertTrue(amb[seq]["can_be_unmatched"])
            self.assertEqual(amb[seq]["outcome_count"], 2)
            self.assertEqual([x["seq"] for x in amb[seq]["partners"]], [1])
        # 导出 JSON 合法、分值有限
        import json
        import math
        self.assertTrue(math.isfinite(res["dp_score"]))
        self.assertNotIn("NaN", json.dumps(res))

    def test_4x1_large_extend_only_endpoints_optimal(self):
        """4 参考层对 1 待校核层：只有首层/末层最优（匹配端层后同侧连跳，
        一个游程 = open + 2·extend）。匹配第 2、3 层都要开两个游程
        （约 -2.6e20，远差于端层），全部排除 -> 计数为 2，歧义区间也只
        挂在首层、末层。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"),
                           (103, "砂岩"), (104, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, self.GO, self.GE)
        self.assertEqual(res["dp_score"], -(self.GO + 2 * self.GE))
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103, 104), ()),
            (((104, 201),), (101, 102, 103), ()),
        })
        self.assertEqual({a["ref_seq"] for a in res["ambiguities"]}, {1, 4})

    def test_1x3_symmetric_only_endpoints_optimal(self):
        """镜像情形 1 对 3：参考层匹配待校核首层/末层（各一个游程）最优，
        匹配中层（两个游程）严格次优 -> 2 个归属。"""
        ref = make_layers([(101, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩"), (203, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, self.GO, self.GE)
        self.assertEqual(res["dp_score"], self.END_SCORE)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (), (202, 203)),
            (((101, 203),), (), (201, 202)),
        })
        # 参考层的歧义候选只有首、末两个待校核层；它在每条最优路径上都
        # 被匹配（整层未匹配要开三个游程），故不可未匹配、结局数为 2
        amb = res["ambiguities"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(sorted(x["seq"] for x in amb[0]["partners"]), [1, 3])
        self.assertFalse(amb[0]["can_be_unmatched"])
        self.assertEqual(amb[0]["outcome_count"], 2)

    def test_equal_huge_open_extend_middle_is_genuine_tie(self):
        """并列回归：open=extend=1e20 时匹配中层也是两个游程 -2e20，
        与端层同分（恰为同一数学值），首/中/末三个归属真正并列，必须
        全部保留——容差收紧不能误杀真正的并列。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1e20)
        self.assertEqual(res["dp_score"], -2e20)
        self.assertEqual(res["solution_count"], 3)
        self.assertEqual(res["solutions_listed"], 3)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103), ()),
            (((102, 201),), (101, 103), ()),
            (((103, 201),), (101, 102), ()),
        })
        # 三个参考层都有“匹配唯一待校核层 / 未匹配”的歧义
        self.assertEqual({a["ref_seq"] for a in res["ambiguities"]},
                         {1, 2, 3})
        for amb in res["ambiguities"]:
            self.assertEqual(amb["outcome_count"], 2)
            self.assertTrue(amb["can_be_unmatched"])

    def test_one_ulp_advantage_still_treated_as_tie(self):
        """端层比中层仅优 1 个 ULP（2e20 处 ULP=32768）：属舍入量级，
        应判为真正并列（3 个归属），验证 ULP 容差不会过紧。"""
        ge = 1e20 - 32768.0           # 端层恰比中层优 1 个 ULP
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, ge)
        self.assertEqual(res["solution_count"], 3)
        self.assertEqual(res["solutions_listed"], 3)
        self.assertEqual({tuple(s["pairs"]) for s in res["tie_solutions"]},
                         {((101, 201),), ((102, 201),), ((103, 201),)})

    def test_fifty_ulp_advantage_is_strict(self):
        """端层比中层优 50 个 ULP：超出路径长度内的舍入累积，严格次优
        的中层被排除（2 个归属）。"""
        ge = 1e20 - 50 * 32768.0
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, ge)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103), ()),
            (((103, 201),), (101, 102), ()),
        })

    def test_nonnegative_penalty_small_scale_unaffected(self):
        """常规小尺度下既有行为不变：正惩罚 2/1 仅全匹配最优；全零 6 并列。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 2.0, 1.0)
        self.assertEqual(res["solution_count"], 1)
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 0.0, 0.0)
        self.assertEqual(res["solution_count"], 6)

    def test_locked_marker_segments_consistent_at_huge_values(self):
        """锁定标志层在大数值下仍为强制锚点：锚点中层锁定 -> 两段 1x1，
        每段匹配（0 分）严格优于未匹配（一次游程），组合后仅 1 个归属。"""
        ref = make_layers([(101, "砂岩"), (102, "煤层"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "煤层"), (203, "砂岩")])
        res = correlate(ref, hole, [(1, 1)], ZERO_WEIGHTS, 5.0,
                        self.GO, self.GE)
        self.assertEqual(res["dp_score"], 0.0)
        self.assertEqual(res["solution_count"], 1)
        self.assertEqual(res["solutions_listed"], 1)
        self.assertEqual(
            [(p["ref_layer_id"], p["hole_layer_id"]) for p in res["pairs"]],
            [(101, 201), (102, 202), (103, 203)])
        # 锚点强制出现且不计未匹配
        self.assertEqual([(a["ref_seq"], a["hole_seq"])
                          for a in res["anchors"]], [(2, 2)])
        self.assertEqual(res["unmatched_ref"], [])
        self.assertEqual(res["unmatched_hole"], [])
        self.assertEqual(res["ambiguities"], [])


class HttpHugeGapPenaltyTest(unittest.TestCase):
    """端到端：PUT /api/settings 接受 1e20 后 POST /api/correlate 不报 500。

    需要 Flask；无 Flask 的最小环境自动跳过（Docker 镜像内必跑）。
    """

    def setUp(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("未安装 Flask，端到端用例在 Docker 内运行")
        import json
        import tempfile
        from pathlib import Path
        from app import db as dbmod
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = dbmod.DB_PATH
        dbmod.DB_PATH = Path(self._tmp.name) / "t.db"
        # init_db 会向空设置表写入 DEFAULT_SETTINGS
        dbmod.init_db()
        from app.routes import app
        self.client = app.test_client()

    def tearDown(self):
        from app import db as dbmod
        dbmod.DB_PATH = self._orig_db_path
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def _hole(self, name, n):
        layers = [
            {"top": i * 2.0, "bottom": (i + 1) * 2.0,
             "lithology": "砂岩", "grain": "中粒", "water": "稍湿",
             "confidence": 0.9}
            for i in range(n)
        ]
        resp = self.client.post("/api/boreholes", json={
            "name": name, "collar_elev": 100, "x": 0 if name == "R" else 10,
            "layers": layers})
        self.assertEqual(resp.status_code, 201, resp.data)
        return resp.get_json()["id"]

    def test_settings_huge_gap_then_correlate_ok(self):
        rid = self._hole("R", 2)
        hid = self._hole("H", 1)
        resp = self.client.put("/api/settings", json={
            "weights": {"lithology": 0, "grain": 0, "water": 0,
                        "elev": 0, "confidence": 0},
            "gap_open": 1e20, "gap_extend": 1})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json()["gap_open"], 1e20)
        resp = self.client.post("/api/correlate",
                                json={"reference_id": rid, "marker_ids": []})
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        h = next(x for x in body["holes"] if x["hole_id"] == hid)
        self.assertEqual(h["dp_score"], -1e20)
        self.assertEqual(h["solution_count"], 2)
        self.assertEqual(h["solutions_listed"], 2)
        self.assertEqual(len(h["tie_solutions"]), 2)
        # 导出归档同样可序列化且包含最近结果
        resp = self.client.get("/api/export")
        self.assertEqual(resp.status_code, 200)
        import json
        exported = json.loads(resp.data)
        eh = next(x for x in exported["last_result"]["holes"]
                  if x["hole_id"] == hid)
        self.assertEqual(eh["dp_score"], -1e20)
        self.assertEqual(eh["solution_count"], 2)
        self.assertEqual(eh["solutions_listed"], 2)

    def test_settings_huge_open_large_extend_no_pseudo_tie(self):
        """任务反例端到端：参考孔三层、待校核孔一层，gap_open=1e20、
        gap_extend=9.999999999e19，中层（须开两次间隙）不得伪并列。"""
        rid = self._hole("R", 3)
        hid = self._hole("H", 1)
        resp = self.client.put("/api/settings", json={
            "weights": {"lithology": 0, "grain": 0, "water": 0,
                        "elev": 0, "confidence": 0},
            "gap_open": 1e20, "gap_extend": 9.999999999e19})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.get_json()["gap_extend"], 9.999999999e19)
        resp = self.client.post("/api/correlate",
                                json={"reference_id": rid, "marker_ids": []})
        self.assertEqual(resp.status_code, 200, resp.data)
        h = next(x for x in resp.get_json()["holes"]
                 if x["hole_id"] == hid)
        # tie_solutions.pairs 携带层 id：建立参考孔 id -> seq 映射
        ref_info = self.client.get("/api/state").get_json()
        ref_bh = next(b for b in ref_info["boreholes"] if b["id"] == rid)
        id_to_seq = {ly["id"]: ly["seq"] for ly in ref_bh["layers"]}
        # 最优：匹配首层或末层，各开一个游程；中层 -2e20 严格次优
        self.assertEqual(h["dp_score"], -1.9999999999e20)
        self.assertEqual(h["solution_count"], 2)
        self.assertEqual(h["solutions_listed"], 2)
        # 中层（参考孔 seq=2）不在任何方案的匹配对中
        for sol in h["tie_solutions"]:
            ref_seqs = {id_to_seq[p[0]] for p in sol["pairs"]}
            self.assertTrue(ref_seqs <= {1, 3})
            self.assertEqual(len(sol["pairs"]), 1)
        # 主解只匹配首层或末层
        self.assertEqual(len(h["pairs"]), 1)
        self.assertIn(h["pairs"][0]["ref_seq"], (1, 3))
        self.assertEqual(len(h["unmatched_ref"]), 2)
        # 歧义区间只挂首层、末层；中层恒未匹配，无歧义
        self.assertEqual(sorted(a["ref_seq"] for a in h["ambiguities"]), [1, 3])
        # 导出归档口径与重算一致
        resp = self.client.get("/api/export")
        self.assertEqual(resp.status_code, 200)
        import json
        exported = json.loads(resp.data)
        eh = next(x for x in exported["last_result"]["holes"]
                  if x["hole_id"] == hid)
        self.assertEqual(eh["dp_score"], -1.9999999999e20)
        self.assertEqual(eh["solution_count"], 2)
        self.assertEqual(eh["solutions_listed"], 2)
        self.assertEqual(sorted(a["ref_seq"] for a in eh["ambiguities"]), [1, 3])


if __name__ == "__main__":
    unittest.main()
