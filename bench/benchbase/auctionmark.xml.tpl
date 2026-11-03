<?xml version="1.0"?>
<!-- bb-bench: auctionmark  bb-terminals: 64
     AuctionMark: eBay-style auctions (users, items, bids, comments, purchases), standard mix, 64 terminals.
     Data changes with time (items close, bids grow); every run starts from the pristine copy. Scale factor per host.
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@ @SF@) and bb_load.sh; from config/mysql/sample_auctionmark_config.xml. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_auctionmark?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
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
            <weights>45, 10, 20, 2, 1, 4, 10, 5, 3</weights>
        </work>
    </works>
        <transactiontypes>
        <transactiontype>
            <name>GetItem</name>
        </transactiontype>
        <transactiontype>
            <name>GetUserInfo</name>
        </transactiontype>
        <transactiontype>
            <name>NewBid</name>
        </transactiontype>
        <transactiontype>
            <name>NewComment</name>
        </transactiontype>
        <transactiontype>
            <name>NewCommentResponse</name>
        </transactiontype>
        <transactiontype>
            <name>NewFeedback</name>
        </transactiontype>
        <transactiontype>
            <name>NewItem</name>
        </transactiontype>
        <transactiontype>
            <name>NewPurchase</name>
        </transactiontype>
        <transactiontype>
            <name>UpdateItem</name>
        </transactiontype>
    </transactiontypes>
</parameters>
