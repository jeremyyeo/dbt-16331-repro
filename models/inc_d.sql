-- models/inc_a.sql (inc_b, inc_c, inc_d identical)
{{ config(materialized="incremental", unique_key="id", incremental_strategy="merge") }}
select id, loaded_at from {{ ref("source_rows") }}
{% if is_incremental() %}
    where id > (select coalesce(max(id), 0) - 10 from {{ this }})
{% endif %}