<?xml version="1.0"?>
<!-- bb-bench: wikipedia  bb-terminals: 64
     Wikipedia: 100k pages, 200k users; sample mix (90% GetPageAnonymous, 7% UpdatePage, 1% GetPageAuthenticated, 2% watch list).
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@) and bb_load.sh. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_wikipedia?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>100</scalefactor>
    <terminals>@TERMINALS@</terminals>
    <works>
        <work>
            <warmup>@WARMUP@</warmup>
            <time>@TIME@</time>
            <rate>unlimited</rate>
            <weights>1,1,7,90,1</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>AddWatchList</name></transactiontype>
        <transactiontype><name>RemoveWatchList</name></transactiontype>
        <transactiontype><name>UpdatePage</name></transactiontype>
        <transactiontype><name>GetPageAnonymous</name></transactiontype>
        <transactiontype><name>GetPageAuthenticated</name></transactiontype>
    </transactiontypes>
</parameters>
