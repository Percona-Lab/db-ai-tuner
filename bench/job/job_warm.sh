#!/bin/bash
# Reset hook of the read-only JOB profile: bring the buffer pool to a comparable start state after a mysqld
# restart by reading title's primary key, the movie_id join indexes and the small dimension tables (~1.5 GB).
# The data set (~9 GB) is larger than a small server's buffer pool, so each run still reads from disk.
DB=${JOB_DB:-imdb}
w() { mysql -N $DB -e "SELECT COUNT(*) FROM $1 FORCE INDEX ($2)" >/dev/null; }
for t in kind_type info_type company_type comp_cast_type role_type link_type keyword company_name movie_link complete_cast movie_info_idx title; do
  w $t PRIMARY
done
w cast_info movie_id_cast_info; w movie_info movie_id_movie_info; w movie_keyword movie_id_movie_keyword
w movie_companies movie_id_movie_companies; w movie_info_idx movie_id_movie_info_idx
exit 0
