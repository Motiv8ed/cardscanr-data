import json

from cardscanr_worldwide.schema import connect
from cardscanr_worldwide.supabase_publication import _batches, build_load_plan, stable_uuid, table_specs


def test_supabase_plan_is_deterministic_and_maps_staging_enums(tmp_path) -> None:
    database = tmp_path / "catalogue.sqlite"
    connection = connect(str(database))
    connection.execute("insert into source_provider values ('p','Provider','official_archive','https://example.test','self_controlled',null,null,'v1')")
    connection.execute("insert into import_run values ('r','p','completed','input',null,'{}','{}','2026-01-01T00:00:00Z','2026-01-01T00:01:00Z',null)")
    connection.execute("insert into source_snapshot values ('s','p','r','input','" + "a" * 64 + "','v1',2,'2026-01-01T00:00:00Z','raw')")
    connection.execute("insert into source_record values ('sr','p','r','s','card','c',null,'input','" + "b" * 64 + "','{}',null)")
    connection.execute("insert into series values ('series','p','series','sr','Series','{}')")
    connection.execute("insert into card_set values ('set','series','p','set','sr','Set','{}','main',1,'{}','{}')")
    connection.execute("insert into set_release values ('release','set','en','US','Set','S','2026-01-01',1,'source','sr')")
    connection.execute("insert into card_design values ('design','pokémon','Pikachu','[25]','design-key')")
    connection.execute("insert into card_printing values ('printing','design','release','sr','1','1',null,null,'Pokémon',null,60,'[\"Lightning\"]',null,'[]','[]','[]','source','{}')")
    connection.execute("insert into card_variant values ('variant','printing','normal',null,null,null,null,0,'{}','recognized')")
    connection.execute("insert into card_localisation values ('printing','en','Pikachu',null,'[]','official')")
    connection.commit()
    specs = {spec.name: spec for spec in table_specs()}
    providers = list(specs["source_providers"].rows(connection))
    designs = list(specs["card_designs"].rows(connection))
    printings = list(specs["card_printings"].rows(connection))
    connection.close()
    assert providers[0]["provider_type"] == "archive"
    assert providers[0]["rights_status"] == "approved_for_mirror"
    assert designs[0]["design_kind"] == "pokemon"
    assert printings[0]["verification_status"] == "verified"
    assert printings[0]["source_record_id"] == stable_uuid("source-record", "sr")
    first = build_load_plan(database)
    second = build_load_plan(database)
    assert first["tables"] == second["tables"]
    assert first["plan_sha256"] == second["plan_sha256"]
    assert first["integrity"] == {"sqlite": "ok", "foreign_key_failures": 0}
    json.dumps(first)


def test_postgrest_batches_have_uniform_keys() -> None:
    batches = list(_batches([{"id": "a", "card_image_id": "c"},
                             {"id": "b", "product_image_id": "p"}], 500))
    assert batches == [[
        {"id": "a", "card_image_id": "c", "product_image_id": None},
        {"id": "b", "card_image_id": None, "product_image_id": "p"},
    ]]
