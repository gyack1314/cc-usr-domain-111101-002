from decimal import Decimal
from datapack.domain import split_interval,prorate,edge_issues
def check(case):
    case.assertEqual(split_interval(0,10,[0,3,3,8,10]),[(0,3),(3,8),(8,10)])
    case.assertEqual(prorate('10','40',0,10,2,7),Decimal(15))
    case.assertIn('EPOCH_CHANGE',edge_issues({'at':0,'meter':'100','epoch':'a'},{'at':10,'meter':'0','epoch':'b'},10,20))
    with case.assertRaises(ValueError):prorate(0,10,0,10,-1,2)
