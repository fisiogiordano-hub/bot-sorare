def crea_controproposta(offer, cards):
    sender = offer.get("sender") or {}

    receiver_slug = str(
        sender.get("slug") or ""
    ).strip()

    if not receiver_slug:
        print(
            "❌ Destinatario non disponibile.",
            flush=True,
        )
        return False

    receive_asset_ids = [
        str(card.get("assetId")).strip()
        for card in cards
        if card.get("assetId")
    ]

    if not receive_asset_ids:
        print(
            "❌ Nessuna carta da ricevere.",
            flush=True,
        )
        return False

    send_amount = len(
        receive_asset_ids
    ) * PAY

    print(
        "\n========================================",
        flush=True,
    )

    print(
        "🟢 CONTROPROPOSTA",
        flush=True,
    )

    print(
        f"👤 Destinatario: {receiver_slug}",
        flush=True,
    )

    print(
        "📥 Carte che riceviamo:",
        flush=True,
    )

    for card in cards:
        print(
            "   🟢 "
            + str(
                card.get("name")
                or card.get("slug")
            ),
            flush=True,
        )

    print(
        f"💰 Pagamento: "
        f"€{send_amount / 100:.2f}",
        flush=True,
    )

    print(
        "🎯 Kulenovic: NON viene ceduto",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    if DRY:
        print(
            "🟡 DRY RUN: "
            "controproposta NON inviata.",
            flush=True,
        )
        return True

    if not STARK:
        print(
            "❌ SORARE_STARK_PRIVATE_KEY "
            "non configurata.",
            flush=True,
        )
        return False

    # ==========================================================
    # PREPARE OFFER
    #
    # IMPORTANTE:
    # receiverSlug NON va più dentro prepareOfferInput.
    # Sorare lo richiede come ARGOMENTO della mutation.
    # ==========================================================

    prepare_mutation = """
        mutation PrepareOffer(
            $input: prepareOfferInput!
            $receiverSlug: String
        ) {
            prepareOffer(
                input: $input
                receiverSlug: $receiverSlug
            ) {
                authorizations {
                    fingerprint

                    request {
                        __typename

                        ... on StarkexLimitOrderAuthorizationRequest {
                            vaultIdSell
                            vaultIdBuy
                            amountSell
                            amountBuy
                            tokenSell
                            tokenBuy
                            nonce
                            expirationTimestamp

                            feeInfo {
                                feeLimit
                                tokenId
                                sourceVaultId
                            }
                        }

                        ... on StarkexTransferAuthorizationRequest {
                            amount
                            condition
                            expirationTimestamp

                            feeInfoUser {
                                feeLimit
                                sourceVaultId
                                tokenId
                            }

                            nonce
                            receiverPublicKey
                            receiverVaultId
                            senderVaultId
                            token
                        }

                        ... on MangopayWalletTransferAuthorizationRequest {
                            nonce
                            amount
                            currency
                            operationHash
                            mangopayWalletId
                        }
                    }
                }

                errors {
                    message
                }
            }
        }
    """

    # ==========================================================
    # QUI NON C'E' PIU' receiverSlug
    # ==========================================================

    prepare_input = {
        "type": "DIRECT_OFFER",

        # NOI NON CEDIAMO KULENOVIC
        "sendAssetIds": [],

        # NOI RICEVIAMO LE CARTE IDONEE
        "receiveAssetIds": receive_asset_ids,

        # NOI PAGHIAMO
        "sendAmount": {
            "amount": str(send_amount),
            "currency": "EUR",
        },

        "clientMutationId": str(
            uuid.uuid4()
        ),
    }

    print(
        "🔧 prepareOffer...",
        flush=True,
    )

    # receiverSlug viene passato SEPARATAMENTE
    data = graphql(
        prepare_mutation,
        {
            "input": prepare_input,
            "receiverSlug": receiver_slug,
        },
    )

    if not data:
        print(
            "❌ prepareOffer fallito.",
            flush=True,
        )
        return False

    result = (
        data.get("data") or {}
    ).get("prepareOffer")

    if not result:
        print(
            "❌ prepareOffer: risposta vuota.",
            flush=True,
        )
        return False

    errors = result.get("errors") or []

    if errors:
        print(
            "❌ prepareOffer rifiutato:",
            flush=True,
        )

        for error in errors:
            print(
                "   - "
                + str(
                    error.get(
                        "message",
                        "Errore sconosciuto",
                    )
                ),
                flush=True,
            )

        return False

    authorizations = (
        result.get("authorizations")
        or []
    )

    if not authorizations:
        print(
            "❌ Nessuna AuthorizationRequest.",
            flush=True,
        )
        return False

    print(
        "✅ prepareOffer riuscito.",
        flush=True,
    )

    print(
        f"🔐 Autorizzazioni: "
        f"{len(authorizations)}",
        flush=True,
    )

    try:
        approvals = firma_con_sorare_crypto(
            authorizations
        )
    except Exception as error:
        print(
            "❌ Firma Stark fallita:",
            flush=True,
        )
        print(
            f"   {error}",
            flush=True,
        )
        return False

    if not approvals:
        print(
            "❌ Nessuna approval generata.",
            flush=True,
        )
        return False

    print(
        f"✅ Approval firmate: "
        f"{len(approvals)}",
        flush=True,
    )

    # ==========================================================
    # CREATE DIRECT OFFER
    # ==========================================================

    create_mutation = """
        mutation CreateDirectOffer(
            $input: createDirectOfferInput!
        ) {
            createDirectOffer(input: $input) {
                tokenOffer {
                    id
                    blockchainId
                    status
                }

                errors {
                    message
                }
            }
        }
    """

    create_input = {
        "approvals": approvals,

        "dealId": str(
            uuid.uuid4()
        ),

        # NOI NON CEDIAMO KULENOVIC
        "sendAssetIds": [],

        # NOI RICEVIAMO LE CARTE IDONEE
        "receiveAssetIds":
            receive_asset_ids,

        # NOI PAGHIAMO
        "sendAmount": {
            "amount": str(send_amount),
            "currency": "EUR",
        },

        "receiverSlug": receiver_slug,

        "clientMutationId": str(
            uuid.uuid4()
        ),
    }

    print(
        "🚀 createDirectOffer...",
        flush=True,
    )

    created = graphql(
        create_mutation,
        {"input": create_input},
    )

    if not created:
        print(
            "❌ createDirectOffer fallito.",
            flush=True,
        )
        return False

    create_result = (
        created.get("data") or {}
    ).get("createDirectOffer")

    if not create_result:
        print(
            "❌ createDirectOffer: "
            "risposta vuota.",
            flush=True,
        )
        return False

    create_errors = (
        create_result.get("errors")
        or []
    )

    if create_errors:
        print(
            "❌ Sorare ha rifiutato "
            "la controproposta:",
            flush=True,
        )

        for error in create_errors:
            print(
                "   - "
                + str(
                    error.get(
                        "message",
                        "Errore sconosciuto",
                    )
                ),
                flush=True,
            )

        return False

    token_offer = (
        create_result.get("tokenOffer")
        or {}
    )

    offer_id = token_offer.get("id")

    if not offer_id:
        print(
            "❌ createDirectOffer non ha "
            "restituito un'offerta.",
            flush=True,
        )
        return False

    print(
        "\n========================================",
        flush=True,
    )

    print(
        "✅ CONTROPROPOSTA INVIATA REALMENTE",
        flush=True,
    )

    print(
        f"🆔 Offerta: {offer_id}",
        flush=True,
    )

    print(
        f"👤 Destinatario: {receiver_slug}",
        flush=True,
    )

    print(
        f"💰 Pagamento: "
        f"€{send_amount / 100:.2f}",
        flush=True,
    )

    print(
        "🎯 Kulenovic NON è stato ceduto.",
        flush=True,
    )

    print(
        "========================================",
        flush=True,
    )

    return True
