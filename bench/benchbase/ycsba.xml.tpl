<?xml version="1.0"?>
<!-- bb-bench: ycsb  bb-terminals: 64
     YCSB workload A: 50% ReadRecord / 50% UpdateRecord, zipfian 0.99, 60M rows (~70 GB, ~2.2x RAM). Reset per run.
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@) and bb_load.sh. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_ycsba?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>60000</scalefactor>
    <skewFactor>0.99</skewFactor>
    <terminals>@TERMINALS@</terminals>
    <works>
        <work>
            <warmup>@WARMUP@</warmup>
            <time>@TIME@</time>
            <rate>unlimited</rate>
            <weights>50,0,0,50,0,0</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>ReadRecord</name></transactiontype>
        <transactiontype><name>InsertRecord</name></transactiontype>
        <transactiontype><name>ScanRecord</name></transactiontype>
        <transactiontype><name>UpdateRecord</name></transactiontype>
        <transactiontype><name>DeleteRecord</name></transactiontype>
        <transactiontype><name>ReadModifyWriteRecord</name></transactiontype>
    </transactiontypes>
</parameters>
