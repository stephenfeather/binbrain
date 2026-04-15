--
-- PostgreSQL database dump
--

\restrict vpVFFsktfQIV2M1GvuawHcsNso8c0KfRpd1sjyh2AieXkscx397M9qfbmTr7CMz

-- Dumped from database version 17.8 (Debian 17.8-0+deb13u1)
-- Dumped by pg_dump version 17.8 (Debian 17.8-0+deb13u1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: bin_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bin_items (
    id bigint NOT NULL,
    bin_id character varying(64),
    item_id bigint,
    confidence double precision,
    quantity double precision,
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: bin_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bin_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bin_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bin_items_id_seq OWNED BY public.bin_items.id;


--
-- Name: bins; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bins (
    bin_id character varying(64) NOT NULL,
    location character varying(255),
    deleted_at timestamp with time zone
);


--
-- Name: item_embeddings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.item_embeddings (
    item_id bigint NOT NULL,
    model text NOT NULL,
    dims integer NOT NULL,
    embedding public.vector(384) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.items (
    item_id bigint NOT NULL,
    name text NOT NULL,
    category text,
    notes text,
    fingerprint text NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: items_item_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.items_item_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: items_item_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.items_item_id_seq OWNED BY public.items.item_id;


--
-- Name: photo_labels; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.photo_labels (
    id bigint NOT NULL,
    photo_id bigint,
    model text NOT NULL,
    label text NOT NULL,
    confidence double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: photo_labels_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.photo_labels_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: photo_labels_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.photo_labels_id_seq OWNED BY public.photo_labels.id;


--
-- Name: photo_objects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.photo_objects (
    id bigint NOT NULL,
    photo_id bigint,
    model text NOT NULL,
    label text NOT NULL,
    confidence double precision NOT NULL,
    x1 double precision NOT NULL,
    y1 double precision NOT NULL,
    x2 double precision NOT NULL,
    y2 double precision NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: photo_objects_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.photo_objects_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: photo_objects_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.photo_objects_id_seq OWNED BY public.photo_objects.id;


--
-- Name: photos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.photos (
    photo_id bigint NOT NULL,
    bin_id character varying(64),
    path text NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: photos_photo_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.photos_photo_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: photos_photo_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.photos_photo_id_seq OWNED BY public.photos.photo_id;


--
-- Name: v_item_bins; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.v_item_bins AS
 SELECT i.item_id,
    i.name,
    i.category,
    bi.bin_id,
    bi.confidence,
    bi.updated_at
   FROM (public.items i
     JOIN public.bin_items bi ON ((bi.item_id = i.item_id)));


--
-- Name: bin_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_items ALTER COLUMN id SET DEFAULT nextval('public.bin_items_id_seq'::regclass);


--
-- Name: items item_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items ALTER COLUMN item_id SET DEFAULT nextval('public.items_item_id_seq'::regclass);


--
-- Name: photo_labels id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_labels ALTER COLUMN id SET DEFAULT nextval('public.photo_labels_id_seq'::regclass);


--
-- Name: photo_objects id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_objects ALTER COLUMN id SET DEFAULT nextval('public.photo_objects_id_seq'::regclass);


--
-- Name: photos photo_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photos ALTER COLUMN photo_id SET DEFAULT nextval('public.photos_photo_id_seq'::regclass);


--
-- Name: bin_items bin_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_items
    ADD CONSTRAINT bin_items_pkey PRIMARY KEY (id);


--
-- Name: bins bins_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bins
    ADD CONSTRAINT bins_pkey PRIMARY KEY (bin_id);


--
-- Name: item_embeddings item_embeddings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_embeddings
    ADD CONSTRAINT item_embeddings_pkey PRIMARY KEY (item_id);


--
-- Name: items items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.items
    ADD CONSTRAINT items_pkey PRIMARY KEY (item_id);


--
-- Name: photo_labels photo_labels_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_labels
    ADD CONSTRAINT photo_labels_pkey PRIMARY KEY (id);


--
-- Name: photo_objects photo_objects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_objects
    ADD CONSTRAINT photo_objects_pkey PRIMARY KEY (id);


--
-- Name: photos photos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photos
    ADD CONSTRAINT photos_pkey PRIMARY KEY (photo_id);


--
-- Name: item_embeddings_hnsw_cos; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX item_embeddings_hnsw_cos ON public.item_embeddings USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: items_fingerprint_uq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX items_fingerprint_uq ON public.items USING btree (fingerprint);


--
-- Name: bin_items bin_items_bin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_items
    ADD CONSTRAINT bin_items_bin_id_fkey FOREIGN KEY (bin_id) REFERENCES public.bins(bin_id) ON DELETE CASCADE;


--
-- Name: bin_items bin_items_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_items
    ADD CONSTRAINT bin_items_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.items(item_id) ON DELETE CASCADE;


--
-- Name: item_embeddings item_embeddings_item_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.item_embeddings
    ADD CONSTRAINT item_embeddings_item_id_fkey FOREIGN KEY (item_id) REFERENCES public.items(item_id) ON DELETE CASCADE;


--
-- Name: photo_labels photo_labels_photo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_labels
    ADD CONSTRAINT photo_labels_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES public.photos(photo_id) ON DELETE CASCADE;


--
-- Name: photo_objects photo_objects_photo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photo_objects
    ADD CONSTRAINT photo_objects_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES public.photos(photo_id) ON DELETE CASCADE;


--
-- Name: photos photos_bin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photos
    ADD CONSTRAINT photos_bin_id_fkey FOREIGN KEY (bin_id) REFERENCES public.bins(bin_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict vpVFFsktfQIV2M1GvuawHcsNso8c0KfRpd1sjyh2AieXkscx397M9qfbmTr7CMz

