<?xml version="1.0"?>
<!-- bb-bench: epinions  bb-terminals: 64
     Epinions: review site (users, items, reviews, trust graph), join-heavy reads + updates, 64 terminals.
     Scale factor per host (2,000 users and 1,000 items per unit).
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@ @SF@) and bb_load.sh; from config/mysql/sample_epinions_config.xml. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_epinions?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>@SF@</scalefactor>
    <terminals>@TERMINALS@</terminals>
    <works>
        <work>
            <warmup>@WARMUP@</warmup>
            <time>@TIME@</time>
            <rate>unlimited</rate>
            <weights>10,10,10,10,10,10,10,10,20</weights>
        </work>
    </works>
        <transactiontypes>
        <transactiontype>
            <name>GetReviewItemById</name>
        </transactiontype>
        <transactiontype>
            <name>GetReviewsByUser</name>
        </transactiontype>
        <transactiontype>
            <name>GetAverageRatingByTrustedUser</name>
        </transactiontype>
        <transactiontype>
            <name>GetItemAverageRating</name>
        </transactiontype>
        <transactiontype>
            <name>GetItemReviewsByTrustedUser</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateUserName</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateItemTitle</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateReviewRating</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateTrustRating</name>
        </transactiontype>
    </transactiontypes>
</parameters>
