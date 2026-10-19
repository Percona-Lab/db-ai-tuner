-- Optional (load.sh with JOB_HISTOGRAMS=1): histograms on the non-indexed columns the 113 queries filter on.
-- Without them the MySQL optimizer guesses fixed selectivities for these predicates (e.g. 10% for =, 11% for LIKE),
-- which is the main source of bad join orders on JOB. Part of the data set, not a tuning knob.
ANALYZE TABLE title UPDATE HISTOGRAM ON production_year, title, episode_nr WITH 64 BUCKETS;
ANALYZE TABLE kind_type UPDATE HISTOGRAM ON kind WITH 16 BUCKETS;
ANALYZE TABLE info_type UPDATE HISTOGRAM ON info WITH 128 BUCKETS;
ANALYZE TABLE company_type UPDATE HISTOGRAM ON kind WITH 8 BUCKETS;
ANALYZE TABLE comp_cast_type UPDATE HISTOGRAM ON kind WITH 8 BUCKETS;
ANALYZE TABLE role_type UPDATE HISTOGRAM ON role WITH 16 BUCKETS;
ANALYZE TABLE link_type UPDATE HISTOGRAM ON link WITH 32 BUCKETS;
ANALYZE TABLE company_name UPDATE HISTOGRAM ON country_code, name WITH 64 BUCKETS;
ANALYZE TABLE keyword UPDATE HISTOGRAM ON keyword WITH 64 BUCKETS;
ANALYZE TABLE movie_info UPDATE HISTOGRAM ON info, note WITH 64 BUCKETS;
ANALYZE TABLE movie_info_idx UPDATE HISTOGRAM ON info WITH 64 BUCKETS;
ANALYZE TABLE movie_companies UPDATE HISTOGRAM ON note WITH 64 BUCKETS;
ANALYZE TABLE cast_info UPDATE HISTOGRAM ON note WITH 64 BUCKETS;
ANALYZE TABLE name UPDATE HISTOGRAM ON gender, name, name_pcode_cf WITH 64 BUCKETS;
ANALYZE TABLE char_name UPDATE HISTOGRAM ON name WITH 64 BUCKETS;
ANALYZE TABLE aka_name UPDATE HISTOGRAM ON name WITH 64 BUCKETS;
ANALYZE TABLE person_info UPDATE HISTOGRAM ON note WITH 64 BUCKETS;
