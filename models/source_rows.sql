-- models/source_rows.sql
{{ config(materialized="table") }}
select id, current_timestamp() as loaded_at from unnest(generate_array(1, 100)) as id
