<?xml version="1.0"?>
<!-- bb-bench: seats  bb-terminals: 64
     SEATS: airline booking (customers, flights, reservations, frequent flyers), standard mix, 64 terminals.
     Scale factor chosen per host for the target size (bench/pipeline/items/benchbase.sh).
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@ @SF@) and bb_load.sh; from config/mysql/sample_seats_config.xml. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_seats?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
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
            <weights>10, 10, 35, 20, 10, 15</weights>
        </work>
    </works>
        <transactiontypes>
        <transactiontype>
            <name>DeleteReservation</name>
        </transactiontype>
        <transactiontype>
            <name>FindFlights</name>
        </transactiontype>
        <transactiontype>
            <name>FindOpenSeats</name>
        </transactiontype>
        <transactiontype>
            <name>NewReservation</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateCustomer</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateReservation</name>
        </transactiontype>
    </transactiontypes>
</parameters>
