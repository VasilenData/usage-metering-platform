-- Run only by the explicit bootstrap command, never by the HTTP application.
CREATE SCHEMA IF NOT EXISTS meter_api;
REVOKE ALL ON SCHEMA meter_api FROM PUBLIC;
GRANT USAGE ON SCHEMA meter_api TO meter_api_runtime, meter_control;
GRANT CREATE ON SCHEMA meter_api TO meter_control;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON public.tenants, public.usage_events, public.prices, public.statements, public.product_events FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM meter_api_runtime;
GRANT USAGE ON SCHEMA public TO meter_api_runtime, meter_control;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.tenants TO meter_control;
GRANT INSERT ON public.prices TO meter_control;
-- Cascaded retention cleanup also needs the referring rows; the NOLOGIN owner
-- is available only through the fixed, bounded provisioning function below.
GRANT SELECT, DELETE ON public.usage_events, public.product_events, public.statements, public.prices TO meter_control;

CREATE OR REPLACE FUNCTION meter_api.current_tenant() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$
 SELECT id FROM public.tenants
 WHERE token_hash = encode(sha256(convert_to(current_setting('app.bearer_token', true), 'UTF8')), 'hex')
   AND scope = 'disposable-sandbox' AND expires_at > statement_timestamp()
$$;

CREATE OR REPLACE FUNCTION meter_api.authenticate(bearer text)
RETURNS TABLE(id text, name text, scope text, expires_at timestamptz, duplicate_events bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$
 SELECT id,name,scope,expires_at,duplicate_events FROM public.tenants
 WHERE length(bearer) BETWEEN 1 AND 256
   AND token_hash=encode(sha256(convert_to(bearer,'UTF8')),'hex')
   AND scope='disposable-sandbox' AND expires_at > statement_timestamp()
$$;

CREATE OR REPLACE FUNCTION meter_api.demo_metadata()
RETURNS TABLE(id text, name text, scope text, expires_at timestamptz, duplicate_events bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$
 SELECT id,name,scope,expires_at,duplicate_events FROM public.tenants
 WHERE id='demo' AND scope='synthetic-demo' AND seed_complete
$$;

CREATE OR REPLACE FUNCTION meter_api.create_sandbox(new_token_hash text)
RETURNS TABLE(id text, name text, scope text, expires_at timestamptz, duplicate_events bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE new_id text := gen_random_uuid()::text;
BEGIN
 IF new_token_hash !~ '^[0-9a-f]{64}$' OR new_token_hash IS NULL THEN
   RAISE EXCEPTION 'Invalid sandbox token digest' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(87326002);
 DELETE FROM public.tenants t WHERE t.scope='disposable-sandbox' AND t.expires_at < clock_timestamp();
 IF (SELECT count(*) FROM public.tenants t WHERE t.scope='disposable-sandbox') >= 64 THEN
   RAISE EXCEPTION 'Sandbox capacity reached' USING ERRCODE='P0001';
 END IF;
 INSERT INTO public.tenants(id,name,token_hash,expires_at,scope)
 VALUES(new_id,'Your disposable sandbox',new_token_hash,clock_timestamp()+interval '1 hour','disposable-sandbox');
 INSERT INTO public.prices(tenant_id,id,effective_from,unit_price_microusd)
 VALUES(new_id,'standard-v1','2026-01-01T00:00:00Z',250),
       (new_id,'standard-v2','2026-10-15T00:00:00Z',200);
 RETURN QUERY SELECT t.id,t.name,t.scope,t.expires_at,t.duplicate_events FROM public.tenants t WHERE t.id=new_id;
END
$$;

CREATE OR REPLACE FUNCTION meter_api.count_duplicate() RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
 UPDATE public.tenants SET duplicate_events=duplicate_events+1
 WHERE id=meter_api.current_tenant()
$$;

ALTER FUNCTION meter_api.current_tenant() OWNER TO meter_control;
ALTER FUNCTION meter_api.authenticate(text) OWNER TO meter_control;
ALTER FUNCTION meter_api.demo_metadata() OWNER TO meter_control;
ALTER FUNCTION meter_api.create_sandbox(text) OWNER TO meter_control;
ALTER FUNCTION meter_api.count_duplicate() OWNER TO meter_control;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA meter_api FROM PUBLIC;
-- A pre-existing helper must not become an accidental HTTP capability.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA meter_api FROM meter_api_runtime;
GRANT EXECUTE ON FUNCTION meter_api.current_tenant(), meter_api.authenticate(text),
 meter_api.demo_metadata(), meter_api.create_sandbox(text), meter_api.count_duplicate() TO meter_api_runtime;
REVOKE CREATE ON SCHEMA meter_api FROM meter_api_runtime, meter_control;

-- app.tenant_id is deliberately ignored. A caller must know the account's
-- unguessable bearer token; choosing a tenant UUID is not authentication.
DO $$
DECLARE relation text;
BEGIN
 FOREACH relation IN ARRAY ARRAY['usage_events','prices','statements','product_events'] LOOP
   EXECUTE format('DROP POLICY IF EXISTS api_read ON public.%I', relation);
   EXECUTE format('DROP POLICY IF EXISTS api_insert ON public.%I', relation);
   EXECUTE format('DROP POLICY IF EXISTS control_rows ON public.%I', relation);
   EXECUTE format('CREATE POLICY api_read ON public.%I FOR SELECT TO meter_api_runtime USING '
      || '(tenant_id=meter_api.current_tenant() OR (tenant_id=''demo'' AND current_setting(''app.demo_read'',true)=''on''))', relation);
   EXECUTE format('CREATE POLICY control_rows ON public.%I TO meter_control USING (true) WITH CHECK (true)', relation);
   EXECUTE format('GRANT SELECT ON public.%I TO meter_api_runtime', relation);
   IF relation <> 'prices' THEN
     EXECUTE format('CREATE POLICY api_insert ON public.%I FOR INSERT TO meter_api_runtime WITH CHECK (tenant_id=meter_api.current_tenant())', relation);
     EXECUTE format('GRANT INSERT ON public.%I TO meter_api_runtime', relation);
   END IF;
 END LOOP;
END
$$;
