<?xml version="1.0"?>
<!-- bb-bench: voter  bb-terminals: 128
     Voter: phone-in voting, one short transaction (check, insert vote, update counts), 128 terminals.
     Commit path and hot-row contention: the vote table grows from empty, scale factor 1.
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@ @SF@) and bb_load.sh; from config/mysql/sample_voter_config.xml. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_voter?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
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
            <weights>100</weights>
        </work>
    </works>
        <transactiontypes>
        <transactiontype>
            <name>Vote</name>
        </transactiontype>
    </transactiontypes>
</parameters>
