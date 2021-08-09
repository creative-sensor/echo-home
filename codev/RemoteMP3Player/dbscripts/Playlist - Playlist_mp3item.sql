CREATE SEQUENCE playlist_id_seq
  INCREMENT 1
  MINVALUE 1
  MAXVALUE 9223372036854775807
  START 1
  CACHE 1;
ALTER TABLE combination_id_seq
  OWNER TO postgres;

CREATE TABLE playlist
(
  id integer NOT NULL DEFAULT nextval('playlist_id_seq'::regclass),
  name text,
  description text,
  CONSTRAINT pk_combi_id PRIMARY KEY (id)
)
WITH (
  OIDS=FALSE
);
ALTER TABLE playlist
  OWNER TO postgres;

