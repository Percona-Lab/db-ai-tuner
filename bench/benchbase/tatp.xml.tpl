<?xml version="1.0"?>
<!-- bb-bench: tatp  bb-terminals: 256
     TATP: 1M subscribers, standard mix (80% read / 20% write), 256 terminals.
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@) and bb_load.sh. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_tatp?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>10</scalefactor>
    <terminals>@TERMINALS@</terminals>
    <works>
        <work>
            <warmup>@WARMUP@</warmup>
            <time>@TIME@</time>
            <rate>unlimited</rate>
            <weights>2,35,10,35,2,14,2</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>DeleteCallForwarding</name></transactiontype>
        <transactiontype><name>GetAccessData</name></transactiontype>
        <transactiontype><name>GetNewDestination</name></transactiontype>
        <transactiontype><name>GetSubscriberData</name></transactiontype>
        <transactiontype><name>InsertCallForwarding</name></transactiontype>
        <transactiontype><name>UpdateLocation</name></transactiontype>
        <transactiontype><name>UpdateSubscriberData</name></transactiontype>
    </transactiontypes>
</parameters>
