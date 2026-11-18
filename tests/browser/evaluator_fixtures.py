import sys,json,pathlib
sys.path.insert(0,str(pathlib.Path('tests').resolve()))
from test_engine import ItemEvaluator
cases={
 'unique':"Item Class: Jewels\nRarity: Unique\nWatcher's Eye\nPrismatic Jewel\n--------\nItem Level: 86\n--------\n4% increased maximum Life",
 'unique_linked':'Item Class: Body Armours\nRarity: Unique\nTabula Rasa\nSimple Robe\n--------\nSockets: W-W-W-W-W-W',
 'unique_rolls':"Item Class: Rings\nRarity: Unique\nVentor's Gamble\nGold Ring\n--------\n+50 to maximum Life\n-30% to Fire Resistance",
 'unique_variant':'Item Class: Body Armours\nRarity: Unique\nSkin of the Lords\nSimple Robe\n--------\nSockets: R-G-B-R-G-B',
 'rare_sparse':ItemEvaluator.BOOTS+'\n ',
 'rare_wide':ItemEvaluator.BOOTS+'\n  ',
 'normal':'Item Class: Body Armours\nRarity: Normal\nVaal Regalia\n--------\nItem Level: 86',
 'unidentified':'Item Class: Body Armours\nRarity: Rare\nVaal Regalia\n--------\nItem Level: 86\n--------\nUnidentified',
 'rare':ItemEvaluator.BOOTS,
 'relaxed':ItemEvaluator.BOOTS+'\n',
 'unknown':'Item Class: Body Armours\nRarity: Rare\nDoom Veil\nVaal Regalia\n--------\nItem Level: 86\n--------\nGrants a completely unknown effect',
 'empty':ItemEvaluator.BOOTS+'\n\n',
 'timeless':ItemEvaluator.LETHAL_PRIDE,
 'magic':'Item Class: Boots\nRarity: Magic\nHealthy Crusader Boots\n--------\nItem Level: 86\n--------\n+80 to maximum Life',
}
out={'texts':cases,'results':{}}
for lang in ['en','ru','de']:
 out['results'][lang]={}
 for name,text in cases.items():
  c=ItemEvaluator();c.setUp()
  found=(lambda p,n:0) if name=='empty' else (lambda p,n:3 if n>=3 else 0) if name=='relaxed' else (lambda p,n:3)
  rows=[{'listing':{'price':{'amount':a,'currency':'chaos'},'account':{'online':{'league':'Allflame'}}},'item':{'typeLine':'Crusader Boots','ilvl':86,'explicitMods':['+80 to maximum Life']}} for a in ([40] if name=='rare_sparse' else [1,10,100] if name=='rare_wide' else [40,65,90])]
  out['results'][lang][name]=c.run_eval(text,found=found,rows=rows,lang=lang)
pathlib.Path(sys.argv[1]).write_text(json.dumps(out,ensure_ascii=True))
print('Generated real evaluator responses with controlled Trade listings')
