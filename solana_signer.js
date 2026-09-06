const fs = require("fs");
const crypto = require("crypto");

const {
    createKeyPairFromPrivateKeyBytes,
    createSignerFromKeyPair,
    createSignableMessage,
    getBase58Decoder,
} = require("@solana/kit");

const { HDKey } = require("micro-key-producer/slip10.js");

const DERIVATION_PATH = "m/44'/501'/0'/0'";


function cleanPrivateKey(value) {

    if (!value) {
        throw new Error(
            "SORARE_STARK_PRIVATE_KEY non configurata"
        );
    }

    let key = String(value).trim();

    if (key.startsWith("0x")) {
        key = key.slice(2);
    }

    if (!/^[0-9a-fA-F]+$/.test(key)) {
        throw new Error(
            "SORARE_STARK_PRIVATE_KEY non è esadecimale"
        );
    }

    if (key.length !== 64) {
        throw new Error(
            "SORARE_STARK_PRIVATE_KEY deve contenere 32 byte"
        );
    }

    return key;
}


async function deriveSolanaSigner(ethereumPrivateKey) {

    const clean = cleanPrivateKey(
        ethereumPrivateKey
    );

    const seed = Buffer.from(
        clean,
        "hex"
    );

    const derived = HDKey
        .fromMasterSeed(seed)
        .derive(DERIVATION_PATH);

    const privateKeyBytes =
        derived.privateKey;

    if (!privateKeyBytes) {
        throw new Error(
            "Derivazione Solana fallita"
        );
    }

    const keyPair =
        await createKeyPairFromPrivateKeyBytes(
            privateKeyBytes
        );

    return createSignerFromKeyPair(
        keyPair
    );
}


async function signSolanaAuthorization(
    ethereumPrivateKey,
    authorization
) {

    if (!authorization) {
        throw new Error(
            "Authorization mancante"
        );
    }

    const r =
        authorization.request;

    if (!r) {
        throw new Error(
            "AuthorizationRequest mancante"
        );
    }

    if (
        r.__typename !==
        "SolanaTokenTransferAuthorizationRequest"
    ) {

        throw new Error(
            "Tipo authorization non supportato: "
            + r.__typename
        );
    }

    const {
        leafIndex,
        merkleTreeAddress,
        originator,
        receiverAddress,
        senderAddress,
        expirationTimestamp,
        nonce,
        transferProxyProgramAddress,
    } = r;


    if (
        leafIndex === undefined ||
        leafIndex === null
    ) {
        throw new Error("leafIndex mancante");
    }

    if (!merkleTreeAddress) {
        throw new Error("merkleTreeAddress mancante");
    }

    if (!originator) {
        throw new Error("originator mancante");
    }

    if (!receiverAddress) {
        throw new Error("receiverAddress mancante");
    }

    if (!senderAddress) {
        throw new Error("senderAddress mancante");
    }

    if (
        expirationTimestamp === undefined ||
        expirationTimestamp === null
    ) {
        throw new Error(
            "expirationTimestamp mancante"
        );
    }

    if (
        nonce === undefined ||
        nonce === null
    ) {
        throw new Error("nonce mancante");
    }

    if (!transferProxyProgramAddress) {
        throw new Error(
            "transferProxyProgramAddress mancante"
        );
    }


    const message = [
        "TRANSFER",
        transferProxyProgramAddress,
        merkleTreeAddress,
        leafIndex.toString(),
        nonce.toString(),
        expirationTimestamp.toString(),
        receiverAddress,
        "0x",
        originator,
    ].join(":");


    console.log(
        "🔐 Solana message:",
        message
    );


    const signer =
        await deriveSolanaSigner(
            ethereumPrivateKey
        );


    if (
        signer.address !==
        senderAddress
    ) {

        throw new Error(
            "CHIAVE SOLANA NON CORRISPONDE AL SENDER ADDRESS. " +
            "Derivata: "
            + signer.address
            + " | Sorare: "
            + senderAddress
        );
    }


    const encoder =
        new TextEncoder();

    const messageBytes =
        encoder.encode(message);


    const messageHash =
        await crypto.webcrypto.subtle.digest(
            "SHA-256",
            messageBytes
        );


    const signableMessage =
        createSignableMessage(
            new Uint8Array(messageHash)
        );


    const signatures =
        await signer.signMessages([
            signableMessage
        ]);


    const signatureBytes =
        signatures[0][signer.address];


    if (!signatureBytes) {
        throw new Error(
            "Firma Solana non restituita"
        );
    }


    const signature =
        getBase58Decoder().decode(
            signatureBytes
        );


    return {

        fingerprint:
            authorization.fingerprint,

        solanaTokenTransferApproval: {

            signature,

            nonce,

            expirationTimestamp,
        },
    };
}


async function main() {

    const input =
        JSON.parse(
            fs.readFileSync(
                0,
                "utf8"
            )
        );


    const privateKey =
        input.privateKey;

    const authorizations =
        input.authorizations || [];


    if (!authorizations.length) {

        throw new Error(
            "Nessuna authorization ricevuta"
        );
    }


    const results = [];


    for (
        const authorization
        of authorizations
    ) {

        const type =
            authorization &&
            authorization.request &&
            authorization.request.__typename;


        if (
            type ===
            "SolanaTokenTransferAuthorizationRequest"
        ) {

            console.error(
                "🔐 Firma Solana authorization"
            );


            const approval =
                await signSolanaAuthorization(
                    privateKey,
                    authorization
                );


            results.push(
                approval
            );


            continue;
        }


        throw new Error(
            "solana_signer.js ha ricevuto " +
            "un authorization non Solana: " +
            type
        );
    }


    process.stdout.write(
        JSON.stringify(results)
    );
}


main().catch(
    error => {

        console.error(
            "❌ SOLANA SIGNER:",
            error.message
        );

        process.exit(1);
    }
);
