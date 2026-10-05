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

第三个反例（大数值伪并列）：gap_open=1e20、gap_extend=9.999999999e19、
参考孔三层对一层时，匹配端层代价 -1.9999999999e20（一个间隙游程），匹配
中层代价 -2e20（前后两个游程），真实分差 1e10；旧的相对容差 1e-9 在该
量级放大到 2e11，把中层次优归属误判为并列（solution_count 曾为 3）。修复
后最优性比较改用 ULP 容差（1e20 量级一个 ULP 约 1.6e4），中层严格出局、
计数与列出均为 2，而同量级下逐位相等的真正并列（如 open=extend=1e20）
仍照常保留。

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

    def test_3x1_near_equal_huge_penalties_middle_not_pseudo_tie(self):
        """任务反例：gap_open=1e20、gap_extend=9.999999999e19。

        匹配端层只开一个间隙游程，代价 -(open+extend)=-1.9999999999e20；
        匹配中层需在匹配前后各开一次间隙（两个游程），代价 -2*open=-2e20。
        真实分差 1e10（约 30 万个 ULP），但旧的相对容差 1e-9 在该量级放大
        到 2e11，把中层误判为并列 -> solution_count 曾为 3。修复后只有首/末
        层两个归属，中层不进主解、并列解或歧义区间。
        """
        go, ge = 1e20, 9.999999999e19
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, go, ge)
        endpoint = -(go + ge)
        middle = -2.0 * go
        # 前提：真实分差远大于 ULP 容差（4096 ULP ≈ 1.34e8），绝非浮点噪声
        self.assertGreater(endpoint - middle, 1e9)
        self.assertEqual(res["dp_score"], endpoint)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102, 103), ()),       # 匹配首层
            (((103, 201),), (101, 102), ()),       # 匹配末层
        })
        # 中层不进主解
        primary = [(p["ref_layer_id"], p["hole_layer_id"]) for p in res["pairs"]]
        self.assertIn(primary, [[(101, 201)], [(103, 201)]])
        self.assertNotIn((102, 201), primary)
        # 中层不进歧义区间：它在任何最优路径上都只能未匹配，结局唯一
        amb_refs = {a["ref_seq"] for a in res["ambiguities"]}
        self.assertEqual(amb_refs, {1, 3})
        for amb in res["ambiguities"]:
            self.assertEqual(amb["outcome_count"], 2)
            self.assertEqual([p["layer_id"] for p in amb["partners"]], [201])
            self.assertTrue(amb["can_be_unmatched"])
        # 中层在任何主解中都只能未匹配（首/末主解的未匹配集合都含中层）
        unmatched = {u["layer_id"] for u in res["unmatched_ref"]}
        self.assertIn(102, unmatched)
        self.assertEqual(unmatched, {102, 103} if primary == [(101, 201)]
                         else {101, 102})

    def test_genuine_tie_preserved_at_huge_scale(self):
        """大数值下真正并列的归属必须保留：2x1、open=extend=1e20 时匹配首/末
        层分值逐位相同（各一个游程，-(open+0)=-1e20），仍是 2 个归属。"""
        ref = make_layers([(101, "砂岩"), (102, "砂岩")])
        hole = make_layers([(201, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1e20)
        self.assertEqual(res["dp_score"], -1e20)
        self.assertEqual(res["solution_count"], 2)
        self.assertEqual(res["solutions_listed"], 2)
        self.assertEqual(signatures(res), {
            (((101, 201),), (102,), ()),
            (((102, 201),), (101,), ()),
        })
        self.assertEqual(len(res["ambiguities"]), 2)

    def test_genuine_tie_preserved_huge_open_small_extend(self):
        """大数值真并列（非对称惩罚）：3x2、open=1e20、extend=1、全零权重。

        三个“相邻对角双匹配 + 一个参考层未匹配游程”的归属代价完全相同：
        无论收尾游程长度是 1（-open）还是 2（-(open+extend)，1 在 1e20
        量级被舍入），均为 -1e20-1；这是真正的并列，必须保留为 3 个归属
        （且每个归属在 ULP 容差下都判得过最优性检查）。
        """
        ref = make_layers([(101, "砂岩"), (102, "砂岩"), (103, "砂岩")])
        hole = make_layers([(201, "砂岩"), (202, "砂岩")])
        res = correlate(ref, hole, [], ZERO_WEIGHTS, 5.0, 1e20, 1.0)
        self.assertEqual(res["dp_score"], -1e20 - 1.0)
        self.assertEqual(res["solution_count"], 3)
        self.assertEqual(res["solutions_listed"], 3)
        self.assertEqual(signatures(res), {
            (((101, 201), (102, 202)), (103,), ()),
            (((101, 201), (103, 202)), (102,), ()),
            (((102, 201), (103, 202)), (101,), ()),
        })
        for amb in res["ambiguities"]:
            self.assertTrue(amb["can_be_unmatched"])

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

    def test_near_equal_huge_penalties_3x1_no_pseudo_tie(self):
        """端到端反例：参考孔三层、待校核孔一层，open=1e20、
        extend=9.999999999e19 -> 中层严格次优，只有首/末层两个归属。"""
        rid = self._hole("R", 3)
        hid = self._hole("H", 1)
        resp = self.client.put("/api/settings", json={
            "weights": {"lithology": 0, "grain": 0, "water": 0,
                        "elev": 0, "confidence": 0},
            "gap_open": 1e20, "gap_extend": 9.999999999e19})
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.client.post("/api/correlate",
                                json={"reference_id": rid, "marker_ids": []})
        self.assertEqual(resp.status_code, 200, resp.data)
        h = next(x for x in resp.get_json()["holes"] if x["hole_id"] == hid)
        endpoint = -(1e20 + 9.999999999e19)
        self.assertEqual(h["dp_score"], endpoint)
        self.assertEqual(h["solution_count"], 2)
        self.assertEqual(h["solutions_listed"], 2)
        matched_ref_seqs = {p["ref_seq"] for s in h["tie_solutions"]
                            for p in s["pairs"]}
        self.assertEqual(matched_ref_seqs, {1, 3})
        # 中层只出现在未匹配侧，歧义区间只覆盖首/末层
        self.assertEqual({a["ref_seq"] for a in h["ambiguities"]}, {1, 3})
        # 主解、并列解、导出 JSON 口径一致
        resp = self.client.get("/api/export")
        self.assertEqual(resp.status_code, 200)
        import json
        eh = next(x for x in json.loads(resp.data)["last_result"]["holes"]
                  if x["hole_id"] == hid)
        self.assertEqual(eh["dp_score"], endpoint)
        self.assertEqual(eh["solution_count"], 2)
        self.assertEqual(eh["solutions_listed"], 2)


class CloseToleranceTest(unittest.TestCase):
    """最优性比较容差：ULP 窗口识别舍入噪声，拒绝多开一次间隙的真实分差。"""

    def test_huge_scale_pseudo_tie_rejected(self):
        from app.correlator import close
        # 任务反例两端：真实分差 1e10（约 30 万 ULP），绝非浮点噪声
        self.assertFalse(close(-(1e20 + 9.999999999e19), -2e20))
        # 反之，几个 ULP 内的舍入差（正/反表独立递推可产生）算并列
        self.assertTrue(close(-2e20, -2e20 + 16 * 32768))

    def test_small_scale_absolute_tolerance(self):
        from app.correlator import close, NEG
        self.assertTrue(close(0.0, 1e-9))
        self.assertFalse(close(0.0, 1e-6))
        self.assertTrue(close(NEG, NEG))
        self.assertFalse(close(NEG, -1e30))
        self.assertFalse(close(-1e30, NEG))


if __name__ == "__main__":
    unittest.main()
