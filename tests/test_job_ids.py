from content_forge.core import EntityKind, new_entity_id, require_entity_id


def test_job_ids_are_part_of_stable_entity_namespace() -> None:
    job_id = new_entity_id(EntityKind.JOB)

    assert job_id.startswith("cf_job_")
    assert require_entity_id(job_id, EntityKind.JOB) == job_id
