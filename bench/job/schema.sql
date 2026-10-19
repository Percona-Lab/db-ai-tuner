-- Join Order Benchmark schema, MySQL port of gregrahn/join-order-benchmark schema.sql.
-- text columns -> VARCHAR sized from the data (max length in the 2013 IMDb CSVs), MEDIUMTEXT for
-- movie_info.info / person_info.info (up to 55k characters). The database uses utf8mb4_0900_bin so that
-- LIKE and = are case-sensitive as in PostgreSQL (the default utf8mb4_0900_ai_ci would change selectivities).
CREATE TABLE aka_name (
    id INT NOT NULL PRIMARY KEY,
    person_id INT NOT NULL,
    name VARCHAR(255) NOT NULL,
    imdb_index VARCHAR(12),
    name_pcode_cf VARCHAR(5),
    name_pcode_nf VARCHAR(5),
    surname_pcode VARCHAR(5),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE aka_title (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    title VARCHAR(600) NOT NULL,
    imdb_index VARCHAR(12),
    kind_id INT NOT NULL,
    production_year INT,
    phonetic_code VARCHAR(5),
    episode_of_id INT,
    season_nr INT,
    episode_nr INT,
    note VARCHAR(100),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE cast_info (
    id INT NOT NULL PRIMARY KEY,
    person_id INT NOT NULL,
    movie_id INT NOT NULL,
    person_role_id INT,
    note VARCHAR(1000),
    nr_order INT,
    role_id INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE char_name (
    id INT NOT NULL PRIMARY KEY,
    name VARCHAR(500) NOT NULL,
    imdb_index VARCHAR(12),
    imdb_id INT,
    name_pcode_nf VARCHAR(5),
    surname_pcode VARCHAR(5),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE comp_cast_type (
    id INT NOT NULL PRIMARY KEY,
    kind VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE company_name (
    id INT NOT NULL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    country_code VARCHAR(255),
    imdb_id INT,
    name_pcode_nf VARCHAR(5),
    name_pcode_sf VARCHAR(5),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE company_type (
    id INT NOT NULL PRIMARY KEY,
    kind VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE complete_cast (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT,
    subject_id INT NOT NULL,
    status_id INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE info_type (
    id INT NOT NULL PRIMARY KEY,
    info VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE keyword (
    id INT NOT NULL PRIMARY KEY,
    keyword VARCHAR(100) NOT NULL,
    phonetic_code VARCHAR(5)
) ENGINE=InnoDB;

CREATE TABLE kind_type (
    id INT NOT NULL PRIMARY KEY,
    kind VARCHAR(15) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE link_type (
    id INT NOT NULL PRIMARY KEY,
    link VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE movie_companies (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    company_id INT NOT NULL,
    company_type_id INT NOT NULL,
    note VARCHAR(255)
) ENGINE=InnoDB;

CREATE TABLE movie_info (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    info_type_id INT NOT NULL,
    info MEDIUMTEXT NOT NULL,
    note VARCHAR(400)
) ENGINE=InnoDB;

CREATE TABLE movie_info_idx (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    info_type_id INT NOT NULL,
    info VARCHAR(32) NOT NULL,
    note VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE movie_keyword (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    keyword_id INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE movie_link (
    id INT NOT NULL PRIMARY KEY,
    movie_id INT NOT NULL,
    linked_movie_id INT NOT NULL,
    link_type_id INT NOT NULL
) ENGINE=InnoDB;

CREATE TABLE name (
    id INT NOT NULL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    imdb_index VARCHAR(12),
    imdb_id INT,
    gender VARCHAR(1),
    name_pcode_cf VARCHAR(5),
    name_pcode_nf VARCHAR(5),
    surname_pcode VARCHAR(5),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;

CREATE TABLE person_info (
    id INT NOT NULL PRIMARY KEY,
    person_id INT NOT NULL,
    info_type_id INT NOT NULL,
    info MEDIUMTEXT NOT NULL,
    note VARCHAR(500)
) ENGINE=InnoDB;

CREATE TABLE role_type (
    id INT NOT NULL PRIMARY KEY,
    role VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE title (
    id INT NOT NULL PRIMARY KEY,
    title VARCHAR(400) NOT NULL,
    imdb_index VARCHAR(12),
    kind_id INT NOT NULL,
    production_year INT,
    imdb_id INT,
    phonetic_code VARCHAR(5),
    episode_of_id INT,
    season_nr INT,
    episode_nr INT,
    series_years VARCHAR(49),
    md5sum VARCHAR(32)
) ENGINE=InnoDB;
