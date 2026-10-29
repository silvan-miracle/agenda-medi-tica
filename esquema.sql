-- esquema.sql · Monitor de agenda mediática (Supabase / Postgres)
-- Pégalo entero en Supabase › SQL Editor y pulsa Run. Se puede ejecutar más de una vez sin romper nada.
--
-- Cinco tablas:
--   medios        los 15 medios
--   tomas         las fotos de portada en crudo (NUNCA se modifican ni se borran)
--   piezas        una fila por pieza, medio y día (aquí trabajas desde R)
--   palabras      frecuencias de palabras por día, ámbito y tema/bloque
--   ejecuciones   resultado de cada recálculo por medio (para ver fallos)

-- ---------------------------------------------------------------- medios
create table if not exists medios (
  id          text primary key,
  nombre      text not null,
  ambito      text not null check (ambito in ('euskadi', 'espana')),
  idioma      text not null default 'es',
  orden       int  not null,
  portada_url text not null
);

-- ---------------------------------------------------------------- tomas (crudo)
create table if not exists tomas (
  id         text primary key,                 -- AAAA-MM-DD__HHMM
  fecha      date        not null,             -- día en Madrid
  hora       text        not null,             -- HH:MM en Madrid
  ts         timestamptz not null,
  n_por_foto int         not null,
  medios     jsonb       not null,             -- la foto tal cual: {medio: {ok, fuente, n, items[], errores[]}}
  cargada_en timestamptz not null default now()
);
create index if not exists tomas_fecha_idx on tomas (fecha);

-- El crudo no se toca: Postgres rechaza cualquier UPDATE o DELETE sobre tomas.
create or replace function tomas_inmutable() returns trigger language plpgsql as $$
begin
  raise exception 'La tabla tomas es de solo inserción (el crudo no se toca)';
end $$;
drop trigger if exists tomas_no_modificar on tomas;
create trigger tomas_no_modificar before update or delete on tomas
  for each row execute function tomas_inmutable();

-- ---------------------------------------------------------------- piezas (una fila por pieza y día)
create table if not exists piezas (
  fecha   date    not null,
  medio   text    not null references medios (id),
  url     text    not null,
  titular text    not null,
  pos     int     not null,                    -- mejor posición en portada ese día (1 = la primera)
  h       text    not null,                    -- primera foto en que aparece: AAAA-MM-DD HH:MM
  fotos   text[]  not null default '{}',       -- horas de las fotos en que estuvo en portada
  temas   text[]  not null default '{}',
  actores text[]  not null default '{}',
  origen  text    not null default 'portadas',
  primary key (fecha, medio, url)
);
create index if not exists piezas_fecha_idx on piezas (fecha);
create index if not exists piezas_temas_idx on piezas using gin (temas);
create index if not exists piezas_actores_idx on piezas using gin (actores);

-- ---------------------------------------------------------------- palabras
create table if not exists palabras (
  fecha   date not null,
  ambito  text not null,                       -- euskadi | espana
  clave   text not null,                       -- tema, o '@bloque' para bloques políticos
  palabra text not null,
  n       int  not null,
  origen  text not null default 'portadas',
  primary key (fecha, ambito, clave, palabra)
);

-- ---------------------------------------------------------------- ejecuciones (fallos)
create table if not exists ejecuciones (
  ejecutado   timestamptz not null,            -- momento del recálculo del día
  medio       text        not null references medios (id),
  fecha       date        not null,            -- día recalculado
  metodo      text        not null default 'portadas',
  ok          boolean     not null,
  n           int         not null default 0,  -- piezas del día
  fotos_ok    int         not null default 0,  -- fotos válidas de este medio
  fotos_total int         not null default 0,  -- fotos del día
  tomas       text[]      not null default '{}',
  errores     text[]      not null default '{}',
  primary key (ejecutado, medio)
);
create index if not exists ejecuciones_fecha_idx on ejecuciones (fecha);

-- ---------------------------------------------------------------- seguridad
-- Supabase expone las tablas por una API pública. Con RLS activado y sin políticas, esa API
-- no puede leer ni escribir nada. La conexión directa (postgres + contraseña, desde Actions y desde R)
-- no se ve afectada.
alter table medios      enable row level security;
alter table tomas       enable row level security;
alter table piezas      enable row level security;
alter table palabras    enable row level security;
alter table ejecuciones enable row level security;

-- ---------------------------------------------------------------- los 15 medios
insert into medios (id, nombre, ambito, idioma, orden, portada_url) values
  ('eitb', 'EITB (Orain)', 'euskadi', 'es', 1, 'https://orain.eus/es'),
  ('elcorreo', 'El Correo', 'euskadi', 'es', 2, 'https://www.elcorreo.com/'),
  ('diariovasco', 'Diario Vasco', 'euskadi', 'es', 3, 'https://www.diariovasco.com/'),
  ('deia', 'Deia', 'euskadi', 'es', 4, 'https://www.deia.eus/'),
  ('noticiasgipuzkoa', 'Noticias de Gipuzkoa', 'euskadi', 'es', 5, 'https://www.noticiasdegipuzkoa.eus/'),
  ('berria', 'Berria', 'euskadi', 'eu', 6, 'https://www.berria.eus/'),
  ('naiz', 'Naiz', 'euskadi', 'es', 7, 'https://www.naiz.eus/es'),
  ('elpais', 'El País', 'espana', 'es', 8, 'https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada'),
  ('elmundo', 'El Mundo', 'espana', 'es', 9, 'https://www.elmundo.es/'),
  ('abc', 'ABC', 'espana', 'es', 10, 'https://www.abc.es/'),
  ('lavanguardia', 'La Vanguardia', 'espana', 'es', 11, 'https://www.lavanguardia.com/'),
  ('eldiario', 'elDiario.es', 'espana', 'es', 12, 'https://www.eldiario.es/'),
  ('elconfidencial', 'El Confidencial', 'espana', 'es', 13, 'https://www.elconfidencial.com/'),
  ('20minutos', '20minutos', 'espana', 'es', 14, 'https://www.20minutos.es/'),
  ('rtve', 'RTVE', 'espana', 'es', 15, 'https://www.rtve.es/noticias/')
on conflict (id) do update set
  nombre = excluded.nombre, ambito = excluded.ambito, idioma = excluded.idioma,
  orden = excluded.orden, portada_url = excluded.portada_url;
