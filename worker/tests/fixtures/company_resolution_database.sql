-- Isolated tests only: mirrors the audited production normalization contract.
create extension if not exists unaccent;
create table public.companies (
 id uuid primary key default gen_random_uuid(),
 name text not null, normalized_name text, domain text,
 created_at timestamptz not null default now(),
 updated_at timestamptz not null default now()
);
create unique index companies_domain_unique on public.companies(lower(domain)) where domain is not null;
CREATE OR REPLACE FUNCTION public.normalize_job_text(input_text text)
 RETURNS text
 LANGUAGE sql
 IMMUTABLE
AS $function$
    select trim(
        regexp_replace(
            lower(
                unaccent(
                    coalesce(input_text, '')
                )
            ),
            '[^a-z0-9]+',
            ' ',
            'g'
        )
    );
$function$
;
CREATE OR REPLACE FUNCTION public.companies_normalize_trigger()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
begin
    new.normalized_name :=
        public.normalize_company_text(new.name);

    return new;
end;
$function$
;
CREATE OR REPLACE FUNCTION public.normalize_company_text(input_text text)
 RETURNS text
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
declare
    value text;
begin
    value := public.normalize_job_text(input_text);

    value := regexp_replace(
        value,
        '\s+(s r o|sro|a s|as|spol s r o|se|ltd|limited|llc|inc|gmbh)$',
        '',
        'i'
    );

    return trim(value);
end;
$function$
;
create trigger companies_normalize_before_write before insert or update of name on public.companies for each row execute function public.companies_normalize_trigger();
