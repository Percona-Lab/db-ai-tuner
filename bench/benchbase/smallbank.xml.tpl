<?xml version="1.0"?>
<!-- bb-bench: smallbank  bb-terminals: 128
     SmallBank: 1M accounts, standard mix (85% write transactions), 128 terminals.
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@) and bb_load.sh. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_smallbank?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>1</scalefactor>
    <terminals>@TERMINALS@</terminals>
    <works>
        <work>
            <warmup>@WARMUP@</warmup>
            <time>@TIME@</time>
            <rate>unlimited</rate>
            <weights>15,15,15,25,15,15</weights>
        </work>
    </works>
    <transactiontypes>
        <transactiontype><name>Amalgamate</name></transactiontype>
        <transactiontype><name>Balance</name></transactiontype>
        <transactiontype><name>DepositChecking</name></transactiontype>
        <transactiontype><name>SendPayment</name></transactiontype>
        <transactiontype><name>TransactSavings</name></transactiontype>
        <transactiontype><name>WriteCheck</name></transactiontype>
    </transactiontypes>
</parameters>
