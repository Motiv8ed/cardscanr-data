# Chinese RC local QA

Generated: 2026-10-14T08:43:14Z

Verdict: **PASS** (14/14 checks)

- [PASS] `pack_card_count` — cards=14923 expected=14923
- [PASS] `pack_set_count` — sets=63
- [PASS] `set_browse_top_sets` — top=[{'set_id': 'pokemon|zh-Hans|CN|pokecardex:CSV10C', 'set_name': 'Chasing Glory Together', 'n': 683}, {'set_id': 'pokemon|zh-Hans|CN|pokecardex:CSV9C', 'set_name': 'Stellar Crystal', 'n': 620}, {'set_id': 'pokemon|zh-Hans|CN|pokecardex:CSV8C', 'set_name': 'Brilliant Fantasy', 'n': 616}, {'set_id': 'pokemon|zh-Hans|CN|pokecardex:CSV7C', 'set_name': 'Blade Awakenings', 'n': 605}, {'set_id': 'pokemon|zh-Hans|CN|pokecardex:CSV95C', 'set_name': 'Terastal Gathering', 'n': 601}]
- [PASS] `search_by_chinese_name_fts` — name='迷你芙' hits=1
- [PASS] `search_by_collector_number` — collector=001/150 hits=90
- [PASS] `set_plus_collector_identity` — set=csm2cc collector=001 hits=1
- [PASS] `variant_rows_present` — samples=[{'set_code': '151C', 'printed_collector_number': '001/151', 'n': 3}, {'set_code': '151C', 'printed_collector_number': '002/151', 'n': 3}, {'set_code': '151C', 'printed_collector_number': '004/151', 'n': 3}, {'set_code': '151C', 'printed_collector_number': '005/151', 'n': 3}, {'set_code': '151C', 'printed_collector_number': '007/151', 'n': 3}]
- [PASS] `placeholders_only` — nonPlaceholder=0 bad=0
- [PASS] `original_877_ids_preserved` — live=877 preserved=877
- [PASS] `binder_identity_fields` — missing_in_sample500=0
- [PASS] `pack_storage_language_zh_cn` — langs={'zh-cn'}
- [PASS] `near_dups_unresolved_preserved` — unresolved=15
- [PASS] `website_pokecardex_attribution` — pages_ok=4 missing=[]
- [PASS] `commercial_metadata_permission_stated` — public_website/data/index.html
