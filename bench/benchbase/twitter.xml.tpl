<?xml version="1.0"?>
<!-- bb-bench: twitter  bb-terminals: 64
     Twitter: 100k users, 4M tweets, zipfian follows; sample mix (90% GetUserTweets, 7% GetFollowers, 1% insert).
     Rendered by bb_run.sh (@WARMUP@ @TIME@ @TERMINALS@) and bb_load.sh. -->
<parameters>
    <type>MYSQL</type>
    <driver>com.mysql.cj.jdbc.Driver</driver>
    <url>jdbc:mysql://localhost:3306/bb_twitter?rewriteBatchedStatements=true&amp;allowPublicKeyRetrieval=True&amp;sslMode=DISABLED</url>
    <username>bb</username>
    <password>bb</password>
    <reconnectOnConnectionFailure>true</reconnectOnConnectionFailure>
    <!-- MySQL's default isolation (the BenchBase samples use SERIALIZABLE, which turns every InnoDB read into a locking read) -->
    <isolation>TRANSACTION_REPEATABLE_READ</isolation>
    <batchsize>1000</batchsize>
    <loaderThreads>12</loaderThreads>
    <scalefactor>200</scalefactor>
    <tracefile>data/twitter/twitter_tweetids.txt</tracefile><tracefile2>data/twitter/twitter_user_ids.txt</tracefile2><base_ip>10.1.</base_ip>
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
        <transactiontype><name>GetTweet</name></transactiontype>
        <transactiontype><name>GetTweetsFromFollowing</name></transactiontype>
        <transactiontype><name>GetFollowers</name></transactiontype>
        <transactiontype><name>GetUserTweets</name></transactiontype>
        <transactiontype><name>InsertTweet</name></transactiontype>
    </transactiontypes>
</parameters>
