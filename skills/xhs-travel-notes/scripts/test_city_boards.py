from unittest import TestCase
from unittest.mock import patch
import json
import city_boards as b


def dp_fixture():
    rows = [dict(shopId=i, shopName=f"馆{i}（东街店）") for i in range(1,13)]
    state = dict(baseInfo=dict(cityName="样城", year=2026),
                 filterState=dict(regionName="全城",categoryName="全部美食",sortName="综合排序"),
                 shopInfo=dict(shopIds=list(range(1,13)),shopList=rows[:2],
                               remainingShopList=list(reversed(rows[2:]))))
    return "window.__K2_INITIAL_STATE__=" + json.dumps(state)


class BoardsTests(TestCase):
    def test_dianping_order_and_wrong_city(self):
        with patch.object(b,"page",return_value=dp_fixture()):
            board=b.dianping("样城",42)
            self.assertEqual([r["position"] for r in board["items"]], list(range(1,11)))
            self.assertEqual(board["items"][9]["name"], "馆10（东街店）")
            with self.assertRaises(ValueError): b.dianping("异城",42)

    def test_duplicate_branch_and_evidence(self):
        def board(key, names):
            return dict(source=key,order="测试",items=[dict(name=n,position=i,url=key+str(i)) for i,n in enumerate(names,1)])
        snap=dict(boards=dict(food=board("高德",["赵记(东门店)","孤店"]),
                              dianping=board("点评",["赵记（东门店）","赵记(西门店)","孤店"])))
        pool=b.shortlist(snap)
        self.assertEqual(len(pool),4)
        self.assertEqual(len(pool[0]["sources"]),2)
        candidate=dict(name="赵记·本地菜（东门店）")
        self.assertTrue(b.corroboration(candidate,"赵记东门店","我们点了炒饵块"))
        self.assertIsNone(b.corroboration(candidate,"赵记西门店","我点了炒饵块"))
        self.assertIsNone(b.corroboration(candidate,"赵记东门店","想去吃它的炒饵块"))
