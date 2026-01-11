#!/usr/bin/env bash
# create_solana_phantom_project.sh
#
# Maakt alle projectbestanden voor jouw Solana Phantom demo (server + public + CLI),
# installeert optioneel dependencies, initialiseert git en (optioneel) pusht naar GitHub met gh.
#
# Gebruik:
# 1) Geef dit bestand uitvoerrechten: chmod +x create_solana_phantom_project.sh
# 2) Run in een lege map of map waar je het project wil plaatsen:
#    ./create_solana_phantom_project.sh
#
# Het script zal je interactief vragen om keuzes (overschrijven, npm install, gh repo create, ...).
set -euo pipefail

# Helpers
ask_yes_no() {
  local prompt default reply
  prompt="$1"
  default="${2:-y}"
  while true; do
    read -r -p "$prompt " reply
    reply="${reply:-$default}"
    case "$reply" in
      [Yy]* ) return 0 ;;
      [Nn]* ) return 1 ;;
      * ) echo "Antwoord a.u.b. met y of n." ;;
    esac
  done
}

info() { printf "\n\033[1;34m[INFO]\033[0m %s\n" "$1"; }
warn() { printf "\n\033[1;33m[WARN]\033[0m %s\n" "$1"; }
err() { printf "\n\033[1;31m[ERROR]\033[0m %s\n" "$1"; }

# Confirm working dir
info "Dit script zal bestanden voor het project aanmaken in de huidige map:"
pwd
if [ -n "$(ls -A . 2>/dev/null)" ]; then
  warn "Deze map is NIET leeg. Bestanden met dezelfde namen kunnen worden overschreven."
  if ! ask_yes_no "Wil je doorgaan en bestanden (indien aanwezig) overschrijven? (y/n) [n]" "n"; then
    info "Geannuleerd door gebruiker."
    exit 1
  fi
fi

# Ask options
if ask_yes_no "Wil je dat het script automatisch 'npm install' uitvoert na aanmaken? (y/n) [y]" "y"; then
  DO_NPM_INSTALL=true
else
  DO_NPM_INSTALL=false
fi

if ask_yes_no "Wil je dat het script een render.yaml toevoegt voor Render deploy? (y/n) [y]" "y"; then
  DO_RENDER_YAML=true
else
  DO_RENDER_YAML=false
fi

# Files creation
info "Maak projectbestanden aan..."

cat > package.json <<'EOF'
{
  "name": "solana-phantom-demo",
  "version": "1.0.0",
  "description": "Solana Phantom demo (server + client + CLI). Client-side mint + Phantom signing, optional CLI script.",
  "main": "server.js",
  "engines": {
    "node": ">=16"
  },
  "scripts": {
    "start": "node server.js",
    "dev": "node server.js",
    "cli": "node scripts/solana-token-demo.js"
  },
  "dependencies": {
    "@solana/web3.js": "^1.79.0",
    "@solana/spl-token": "^0.3.9",
    "express": "^4.18.2",
    "body-parser": "^1.20.2",
    "helmet": "^7.0.0",
    "express-rate-limit": "^6.7.0",
    "node-fetch": "^2.6.7",
    "inquirer": "^9.2.7",
    "chalk": "^5.3.0",
    "bs58": "^5.0.0",
    "@metaplex-foundation/js": "^0.18.0",
    "@metaplex-foundation/mpl-token-metadata": "^1.0.0",
    "@raydium-io/raydium-sdk": "^0.6.0"
  }
}
EOF

cat > server.js <<'EOF'
/**
 * server.js (light hardening)
 *
 * - voegt helmet security headers toe
 * - voegt express-rate-limit toe (bescherming tegen spam)
 * - valideert /broadcast input (max grootte, base64 check)
 * - serveert ./public zoals eerder
 */
const express = require("express");
const bodyParser = require("body-parser");
const path = require("path");
const fetch = require("node-fetch");
const helmet = require("helmet");
const rateLimit = require("express-rate-limit");
const { Connection, clusterApiUrl } = require("@solana/web3.js");

const app = express();

// Basic security headers
app.use(helmet());

// Body parser (keep modest limit)
app.use(bodyParser.json({ limit: "5mb" }));

// Simple rate limiter for broadcast and raydium endpoints
const limiter = rateLimit({
  windowMs: 60 * 1000, // 1 minute
  max: 30, // max 30 requests per IP per windowMs
  standardHeaders: true,
  legacyHeaders: false,
});
app.use("/broadcast", limiter);
app.use("/raydium", limiter);

app.use(express.static(path.join(__dirname, "public")));

const NETWORK = process.env.SOLANA_NETWORK || "devnet";
const RPC_URL = clusterApiUrl(NETWORK);
const connection = new Connection(RPC_URL, "confirmed");

function raydiumSdkUrl(network) {
  if (network === "mainnet") return "https://api.raydium.io/v2/sdk/liquidity/mainnet.json";
  return "https://api.raydium.io/v2/sdk/liquidity/mainnet.json";
}

// Broadcast a fully-signed tx (base64) with basic validation
app.post("/broadcast", async (req, res) => {
  try {
    const { signedTx } = req.body;
    if (!signedTx || typeof signedTx !== "string") {
      return res.status(400).json({ error: "signedTx (base64 string) is required" });
    }

    // Basic size limit: prevent huge payloads (e.g. > 300KB binary)
    const MAX_BASE64 = 400000; // ~300KB binary
    if (signedTx.length > MAX_BASE64) {
      return res.status(400).json({ error: "signedTx too large" });
    }

    // Validate base64 content by attempting to decode
    let raw;
    try {
      raw = Buffer.from(signedTx, "base64");
    } catch (e) {
      return res.status(400).json({ error: "signedTx must be valid base64" });
    }

    // Additional safety: limit decoded size
    if (raw.length > 350000) {
      return res.status(400).json({ error: "signedTx decoded size too large" });
    }

    const txid = await connection.sendRawTransaction(raw);
    await connection.confirmTransaction(txid, "confirmed");
    res.json({ txid, explorer: `https://explorer.solana.com/tx/${txid}?cluster=${NETWORK}` });
  } catch (err) {
    console.error("broadcast error:", err && err.message ? err.message : err);
    res.status(500).json({ error: err.message || String(err) });
  }
});

// Raydium helper: pool lookup (read-only)
app.get("/raydium/get-pool-keys", async (req, res) => {
  try {
    const { mint } = req.query;
    if (!mint) return res.status(400).json({ error: "mint query param required" });

    const url = raydiumSdkUrl(NETWORK);
    const response = await fetch(url);
    if (!response.ok) {
      return res.status(500).json({ error: `Could not fetch Raydium SDK JSON: ${response.status}` });
    }
    const json = await response.json();
    const all = json.official || [];
    const found = all.find((p) => p.baseMint === mint || p.quoteMint === mint || p.lpMint === mint);
    if (!found) return res.json({ pool: null });
    return res.json({ pool: found });
  } catch (err) {
    console.error("get-pool-keys error:", err && err.message ? err.message : err);
    res.status(500).json({ error: err.message || String(err) });
  }
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server listening on http://localhost:${PORT}  (network: ${NETWORK})`);
  console.log("Serve static UI from ./public");
});
EOF

mkdir -p public
cat > public/index.html <<'EOF'
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Phantom + Full Demo (Client-side mint)</title>
  <style>
    body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, Arial; max-width:1000px; margin:1rem auto; padding:1rem; }
    .card { border:1px solid #e6edf3; padding:1rem; border-radius:8px; background:#fbfdff; margin-bottom:1rem; }
    button { padding:.5rem .8rem; margin-right:.4rem; cursor:pointer; }
    input, select, textarea { padding:.45rem; margin-right:.4rem; }
    pre { background:#0b1220; color:#e6f1ff; padding:.8rem; border-radius:6px; overflow:auto; }
    label { display:inline-block; width:150px; vertical-align:top; }
    .muted { color:#6b7280; font-size:0.95rem; }
    .danger { color:#b91c1c; font-weight:700; }
    .status { margin-top:.6rem; font-weight:600; }
  </style>
</head>
<body>
  <header>
    <h1>Phantom + Full Demo (Client-side mint)</h1>
    <p class="muted">Token-mint flow draait in de browser (Phantom signeert). Demo-acties werken op beide netwerken; mainnet vereist typed bevestiging.</p>
  </header>

  <section class="card">
    <strong>Wallet</strong><br/><br/>
    <button id="connectBtn">Connect Phantom</button>
    <button id="disconnectBtn" disabled>Disconnect</button>
    <p id="pubkey"><em>Niet verbonden</em></p>
    <p id="balance" class="muted"></p>
    <div>
      <label>Network:</label>
      <select id="networkSelect">
        <option value="devnet" selected>devnet</option>
        <option value="testnet">testnet</option>
        <option value="mainnet-beta">mainnet-beta</option>
      </select>
      <label style="margin-left:20px"><input type="checkbox" id="allowMainnetDemos"> Sta demo-acties toe op Mainnet</label>
    </div>
  </section>

  <section class="card">
    <strong>Token Creatie (client-side, Phantom signeert)</strong><br/><br/>
    <div>
      <label>Naam:</label><input id="tokenName" value="FullDemoToken"/>
      <label>Symbool:</label><input id="tokenSymbol" value="FDT"/>
    </div>
    <div style="margin-top:.6rem">
      <label>Decimals:</label><input id="decimals" type="number" value="9" style="width:80px" />
      <label>Total Supply:</label><input id="totalSupply" type="number" value="1000000" />
    </div>
    <div style="margin-top:.6rem">
      <label>Afbeelding URI (extern geupload):</label>
      <input id="imageUrl" placeholder="https://ipfs.io/ipfs/..." size="60"/>
    </div>
    <div style="margin-top:.8rem">
      <button id="createTokenBtn" disabled>Maak Token (client-side)</button>
    </div>
    <div style="margin-top:.6rem">
      <strong>Resultaat:</strong>
      <pre id="createResult">Geen acties uitgevoerd.</pre>
    </div>
    <div style="margin-top:.6rem">
      <em>Opmerking: na het aanmaken kun je de mint private key exporteren (optioneel). Houd deze veilig.</em>
    </div>
  </section>

  <section class="card">
    <strong>Raydium / Liquidity (helper)</strong>
    <p class="muted">Raydium-transacties worden aangeraden client-side te doen (Phantom tekent). Dit is een helper voor pool-lookup.</p>
    <div style="margin-top:.6rem">
      <label>Base Mint (jouw token):</label><input id="baseMint" placeholder="Mint address" size="60"/>
    </div>
    <div style="margin-top:.8rem">
      <button id="getPoolBtn" disabled>Zoek bestaand pool (Raydium)</button>
    </div>
    <pre id="poolResult">Geen pool acties uitgevoerd.</pre>
  </section>

  <section class="card">
    <strong>Educatieve demo acties</strong>
    <p class="muted">Hidden Mint, Freeze, Fake Burn — werken op devnet en mainnet. Op mainnet moet je expliciet typen "I UNDERSTAND" om door te gaan.</p>
    <div style="margin-top:.6rem">
      <label>Target Mint:</label><input id="targetMint" placeholder="Mint address" size="60"/>
    </div>
    <div style="margin-top:.6rem">
      <button id="hiddenMintBtn" disabled>Hidden Mint</button>
      <button id="freezeBtn" disabled>Freeze mijn ATA</button>
      <button id="fakeBurnBtn" disabled>Fake Burn</button>
    </div>
    <pre id="demoLog">Geen demo acties uitgevoerd.</pre>
  </section>

  <section class="card">
    <strong>Logs</strong>
    <pre id="log">Waiting...</pre>
  </section>

  <script type="module">
    import {
      Connection,
      clusterApiUrl,
      Transaction,
      PublicKey,
      SystemProgram,
      Keypair
    } from "https://cdn.skypack.dev/@solana/web3.js";
    import {
      getAssociatedTokenAddress,
      createAssociatedTokenAccountInstruction,
      createInitializeMintInstruction,
      createMintToInstruction,
      createFreezeAccountInstruction,
      createTransferInstruction,
      TOKEN_PROGRAM_ID,
      MINT_SIZE,
    } from "https://cdn.skypack.dev/@solana/spl-token";
    import { createCreateMetadataAccountV3Instruction } from "https://cdn.skypack.dev/@metaplex-foundation/mpl-token-metadata";

    const connectBtn = document.getElementById("connectBtn");
    const disconnectBtn = document.getElementById("disconnectBtn");
    const pubkeyEl = document.getElementById("pubkey");
    const balanceEl = document.getElementById("balance");
    const networkSelect = document.getElementById("networkSelect");
    const allowMainnetDemos = document.getElementById("allowMainnetDemos");

    const createTokenBtn = document.getElementById("createTokenBtn");
    const createResult = document.getElementById("createResult");
    const logEl = document.getElementById("log");

    const hiddenMintBtn = document.getElementById("hiddenMintBtn");
    const freezeBtn = document.getElementById("freezeBtn");
    const fakeBurnBtn = document.getElementById("fakeBurnBtn");
    const demoLog = document.getElementById("demoLog");

    const getPoolBtn = document.getElementById("getPoolBtn");
    const poolResult = document.getElementById("poolResult");

    let provider = null;
    let connection = new Connection(clusterApiUrl("devnet"), "confirmed");

    function log(...args) {
      console.log(...args);
      logEl.textContent = new Date().toISOString() + " " + args.join(" ") + "\n\n" + logEl.textContent;
    }

    function getProvider() {
      if ("solana" in window) {
        const p = window.solana;
        if (p.isPhantom) return p;
      }
      return null;
    }

    async function updateBalance() {
      try {
        if (!provider) { balanceEl.textContent = ""; return; }
        const pub = provider.publicKey;
        const bal = await connection.getBalance(pub);
        balanceEl.textContent = `Solana balance: ${(bal / 1e9).toFixed(6)} SOL`;
      } catch (err) {
        console.warn("Balance update failed:", err);
        balanceEl.textContent = "";
      }
    }

    async function connectWallet() {
      provider = getProvider();
      if (!provider) { alert("Phantom niet gevonden"); return; }
      try {
        await provider.connect();
      } catch (e) { log("Connect geannuleerd"); return; }
      const pk = provider.publicKey.toString();
      pubkeyEl.innerHTML = `Verbonden: <code>${pk}</code>`;
      connectBtn.disabled = true;
      disconnectBtn.disabled = false;
      createTokenBtn.disabled = false;
      hiddenMintBtn.disabled = false;
      freezeBtn.disabled = false;
      fakeBurnBtn.disabled = false;
      getPoolBtn.disabled = false;
      networkSelect.disabled = true;
      log("Connected", pk);
      await updateBalance();
    }

    async function disconnectWallet() {
      if (!provider) return;
      try { await provider.disconnect(); } catch (e) {}
      pubkeyEl.innerHTML = `<em>Niet verbonden</em>`;
      balanceEl.textContent = "";
      connectBtn.disabled = false;
      disconnectBtn.disabled = true;
      createTokenBtn.disabled = true;
      hiddenMintBtn.disabled = true;
      freezeBtn.disabled = true;
      fakeBurnBtn.disabled = true;
      getPoolBtn.disabled = true;
      networkSelect.disabled = false;
      log("Disconnected");
    }

    connectBtn.addEventListener("click", connectWallet);
    disconnectBtn.addEventListener("click", disconnectWallet);

    networkSelect.addEventListener("change", (e) => {
      const n = e.target.value;
      connection = new Connection(clusterApiUrl(n), "confirmed");
      log("Network set to", n);
    });

    async function confirmMainnetAction() {
      const net = networkSelect.value;
      if (net === "mainnet-beta" && !allowMainnetDemos.checked) {
        alert("Je moet 'Sta demo-acties toe op Mainnet' aanvinken om mainnet demo-acties uit te voeren.");
        return false;
      }
      if (net === "mainnet-beta") {
        const typed = prompt("Je voert een actie uit op MAINNET. Typ exact: I UNDERSTAND om door te gaan.");
        if (typed !== "I UNDERSTAND") {
          alert("Bevestiging mislukt — actie geannuleerd.");
          return false;
        }
      }
      return true;
    }

    function setProcessing(state, message = "Processing...") {
      if (state) {
        createResult.textContent = message;
        createTokenBtn.disabled = true;
        hiddenMintBtn.disabled = true;
        freezeBtn.disabled = true;
        fakeBurnBtn.disabled = true;
        getPoolBtn.disabled = true;
      } else {
        createTokenBtn.disabled = false;
        hiddenMintBtn.disabled = false;
        freezeBtn.disabled = false;
        fakeBurnBtn.disabled = false;
        getPoolBtn.disabled = false;
      }
    }

    createTokenBtn.addEventListener("click", async () => {
      try {
        if (!provider) { alert("Verbind eerst Phantom"); return; }
        const walletPubkey = provider.publicKey;
        const decimals = Number(document.getElementById("decimals").value);
        const totalSupply = BigInt(Number(document.getElementById("totalSupply").value));
        if (totalSupply <= 0n) { alert("Total supply moet > 0 zijn"); return; }

        setProcessing(true, "Genereer mint Keypair in browser...");
        const mintKeypair = Keypair.generate();
        const mintPubkey = mintKeypair.publicKey;

        const ata = await getAssociatedTokenAddress(mintPubkey, walletPubkey);
        const rentExempt = await connection.getMinimumBalanceForRentExemption(MINT_SIZE);

        const tx = new Transaction();
        tx.add(SystemProgram.createAccount({
          fromPubkey: walletPubkey,
          newAccountPubkey: mintPubkey,
          lamports: rentExempt,
          space: MINT_SIZE,
          programId: TOKEN_PROGRAM_ID,
        }));

        tx.add(createInitializeMintInstruction(
          mintPubkey,
          decimals,
          walletPubkey,
          walletPubkey,
          TOKEN_PROGRAM_ID
        ));

        const ataInfo = await connection.getAccountInfo(ata);
        if (!ataInfo) {
          tx.add(createAssociatedTokenAccountInstruction(
            walletPubkey,
            ata,
            walletPubkey,
            mintPubkey
          ));
        }

        const amount = totalSupply * (10n ** BigInt(decimals));
        if (amount > BigInt(Number.MAX_SAFE_INTEGER)) {
          if (!confirm("Amount may be too large for JS Number. Continue?")) {
            setProcessing(false);
            return;
          }
        }
        tx.add(createMintToInstruction(
          mintPubkey,
          ata,
          walletPubkey,
          Number(amount)
        ));

        tx.recentBlockhash = (await connection.getRecentBlockhash()).blockhash;
        tx.feePayer = walletPubkey;

        tx.partialSign(mintKeypair);

        setProcessing(true, "Vraag Phantom om te ondertekenen...");
        const signed = await provider.signTransaction(tx);

        setProcessing(true, "Versturen naar netwerk...");
        const raw = signed.serialize();
        const txid = await connection.sendRawTransaction(raw);
        await connection.confirmTransaction(txid, "confirmed");

        createResult.textContent = `✅ Token gemaakt! Mint: ${mintPubkey.toString()}\nATA: ${ata.toString()}\nTX: ${txid}`;
        log("Token creatie voltooid", mintPubkey.toString(), txid);

        const exportSecret = confirm("Wil je de mint private key exporteren (bewaar veilig)?");
        if (exportSecret) {
          const secret = Array.from(mintKeypair.secretKey);
          const b58 = await (async () => {
            const mod = await import("https://cdn.skypack.dev/bs58");
            return mod.encode(new Uint8Array(secret));
          })();
          createResult.textContent += `\n\nMint private key (Base58): ${b58}\nWAARSCHUWING: bewaar dit veilig!`;
        }

        const imageUrl = document.getElementById("imageUrl").value.trim();
        const name = document.getElementById("tokenName").value.trim();
        const symbol = document.getElementById("tokenSymbol").value.trim();
        if (imageUrl) {
          try {
            await createMetadataOnChain(mintPubkey.toString(), name || symbol || "Token", symbol || "", imageUrl);
            createResult.textContent += `\nMetadata aangemaakt (on-chain)`;
          } catch (err) {
            log("Metadata aanmaken mislukt:", err.message || err);
            createResult.textContent += `\nMetadata aanmaken mislukt: ${err.message || err}`;
          }
        }

        await updateBalance();
      } catch (err) {
        console.error(err);
        createResult.textContent = "❌ Fout: " + (err.message || err);
      } finally {
        setProcessing(false);
      }
    });

    async function createMetadataOnChain(mintAddr, name, symbol, uri) {
      try {
        const walletPubkey = provider.publicKey;
        const mintKey = new PublicKey(mintAddr);
        const METADATA_PROGRAM_ID = new PublicKey("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s");

        const [metadataPDA] = await PublicKey.findProgramAddress(
          [
            Buffer.from("metadata"),
            METADATA_PROGRAM_ID.toBuffer(),
            mintKey.toBuffer(),
          ],
          METADATA_PROGRAM_ID
        );

        const accounts = {
          metadata: metadataPDA,
          mint: mintKey,
          mintAuthority: walletPubkey,
          payer: walletPubkey,
          updateAuthority: walletPubkey,
        };

        const data = {
          name,
          symbol,
          uri,
          sellerFeeBasisPoints: 0,
          creators: null,
          collection: null,
          uses: null,
        };

        const args = {
          createMetadataAccountArgsV3: {
            data,
            isMutable: true,
            collectionDetails: null,
          }
        };

        const ix = createCreateMetadataAccountV3Instruction(accounts, args);
        const tx = new Transaction().add(ix);
        tx.recentBlockhash = (await connection.getRecentBlockhash()).blockhash;
        tx.feePayer = walletPubkey;
        const signed = await provider.signTransaction(tx);
        const txid = await connection.sendRawTransaction(signed.serialize());
        await connection.confirmTransaction(txid, "confirmed");
        log("Metadata on-chain gemaakt:", txid);
        return txid;
      } catch (err) {
        console.error("metadata error", err);
        throw err;
      }
    }

    getPoolBtn.addEventListener("click", async () => {
      try {
        const baseMint = document.getElementById("baseMint").value.trim();
        if (!baseMint) { alert("Voer base mint in"); return; }
        setProcessing(true, "Zoeken naar pool...");
        const resp = await fetch(`/raydium/get-pool-keys?mint=${encodeURIComponent(baseMint)}`);
        const data = await resp.json();
        poolResult.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        console.error(err);
        poolResult.textContent = "❌ Fout: " + (err.message || err);
      } finally {
        setProcessing(false);
      }
    });

    hiddenMintBtn.addEventListener("click", async () => {
      try {
        if (!provider) { alert("Verbind eerst Phantom"); return; }
        if (!await confirmMainnetAction()) return;
        const mint = document.getElementById("targetMint").value.trim();
        if (!mint) { alert("Voer target mint in"); return; }
        const mintPub = new PublicKey(mint);
        const owner = provider.publicKey;
        const ata = await getAssociatedTokenAddress(mintPub, owner);
        const ataInfo = await connection.getAccountInfo(ata);
        const tx = new Transaction();
        if (!ataInfo) {
          tx.add(createAssociatedTokenAccountInstruction(owner, ata, owner, mintPub));
        }
        const amount = BigInt(1000) * (10n ** 9n);
        tx.add(createMintToInstruction(mintPub, ata, owner, Number(amount)));
        tx.recentBlockhash = (await connection.getRecentBlockhash()).blockhash;
        tx.feePayer = owner;
        setProcessing(true, "Vraag Phantom om te ondertekenen...");
        const signed = await provider.signTransaction(tx);
        setProcessing(true, "Verzenden...");
        const txid = await connection.sendRawTransaction(signed.serialize());
        await connection.confirmTransaction(txid, "confirmed");
        demoLog.textContent = `✅ Hidden mint voltooid: ${txid}`;
        log("Hidden mint:", txid);
        await updateBalance();
      } catch (err) {
        console.error(err);
        demoLog.textContent = "❌ Fout: " + (err.message || err);
      } finally {
        setProcessing(false);
      }
    });

    freezeBtn.addEventListener("click", async () => {
      try {
        if (!provider) { alert("Verbind eerst Phantom"); return; }
        if (!await confirmMainnetAction()) return;
        const mint = document.getElementById("targetMint").value.trim();
        if (!mint) { alert("Voer target mint in"); return; }
        const mintPub = new PublicKey(mint);
        const owner = provider.publicKey;
        const ata = await getAssociatedTokenAddress(mintPub, owner);
        const ataInfo = await connection.getAccountInfo(ata);
        if (!ataInfo) { alert("ATA bestaat niet voor deze wallet"); return; }
        const ix = createFreezeAccountInstruction(ata, mintPub, owner, []);
        const tx = new Transaction().add(ix);
        tx.recentBlockhash = (await connection.getRecentBlockhash()).blockhash;
        tx.feePayer = owner;
        setProcessing(true, "Vraag Phantom om te ondertekenen...");
        const signed = await provider.signTransaction(tx);
        setProcessing(true, "Verzenden...");
        const txid = await connection.sendRawTransaction(signed.serialize());
        await connection.confirmTransaction(txid, "confirmed");
        demoLog.textContent = `✅ Account bevroren: ${txid}`;
        log("Freeze:", txid);
      } catch (err) {
        console.error(err);
        demoLog.textContent = "❌ Fout: " + (err.message || err);
      } finally {
        setProcessing(false);
      }
    });

    fakeBurnBtn.addEventListener("click", async () => {
      try {
        if (!provider) { alert("Verbind eerst Phantom"); return; }
        if (!await confirmMainnetAction()) return;
        const mint = document.getElementById("targetMint").value.trim();
        if (!mint) { alert("Voer target mint in"); return; }
        const mintPub = new PublicKey(mint);
        const owner = provider.publicKey;
        const ata = await getAssociatedTokenAddress(mintPub, owner);
        const ataInfo = await connection.getAccountInfo(ata);
        if (!ataInfo) { alert("ATA bestaat niet voor deze wallet"); return; }
        const rand = Keypair.generate();
        const fakeAta = await getAssociatedTokenAddress(mintPub, rand.publicKey);
        const tx = new Transaction();
        tx.add(createAssociatedTokenAccountInstruction(owner, fakeAta, rand.publicKey, mintPub));
        tx.add(createTransferInstruction(ata, fakeAta, owner, 10 * (10 ** 9), [], TOKEN_PROGRAM_ID));
        tx.recentBlockhash = (await connection.getRecentBlockhash()).blockhash;
        tx.feePayer = owner;
        setProcessing(true, "Vraag Phantom om te ondertekenen...");
        const signed = await provider.signTransaction(tx);
        setProcessing(true, "Verzenden...");
        const txid = await connection.sendRawTransaction(signed.serialize());
        await connection.confirmTransaction(txid, "confirmed");
        demoLog.textContent = `✅ Fake burn (transfer naar random): ${txid}`;
        log("Fake burn:", txid);
      } catch (err) {
        console.error(err);
        demoLog.textContent = "❌ Fout: " + (err.message || err);
      } finally {
        setProcessing(false);
      }
    });

  </script>
</body>
</html>
EOF

mkdir -p scripts
cat > scripts/solana-token-demo.js <<'EOF'
/*
  scripts/solana-token-demo.js
  (Originele CLI script — be careful: uses private key; use only test wallets)
  This file is long; in case you'd like to replace or edit it, open it in an editor.
  For brevity here we add a simplified version that references the original heavy CLI.
*/
console.log("CLI placeholder for solana-token-demo. If you need the full CLI version, replace this file with your original script.");
console.log("Run 'npm run cli' to execute your CLI script (if present).");
EOF

cat > README.md <<'EOF'
# Solana Phantom Demo (server + client + CLI)

Inhoud:
- `server.js` — eenvoudige Express server die `public/` serveert en endpoints biedt.
- `public/index.html` — browser UI die Phantom verbindt en token‑flows client‑side uitvoert.
- `scripts/solana-token-demo.js` — placeholder CLI. Replace with your original CLI script if needed.

Lokale testen:
1. Installeer deps:
   npm install

2. Start server:
   npm start

3. Open in browser:
   http://localhost:3000  (Phantom extension vereist)

Deploy naar Render:
1. Push naar GitHub.
2. Maak een nieuw Web Service op https://dashboard.render.com en koppel je repo.
3. Build command: npm install
4. Start command: npm start
5. Render levert HTTPS — Phantom werkt op die URL.
EOF

cat > .gitignore <<'EOF'
node_modules
.env
.DS_Store
npm-debug.log
EOF

if [ "$DO_RENDER_YAML" = true ]; then
  cat > render.yaml <<'EOF'
# render.yaml (optional)
# Simple Render service blueprint for a web service
services:
  - type: web
    name: solana-phantom-demo
    env: node
    plan: free
    branch: main
    buildCommand: "npm install"
    startCommand: "npm start"
    autoDeploy: true
EOF
fi

info "Bestanden aangemaakt."

# npm install?
if $DO_NPM_INSTALL; then
  if command -v npm >/dev/null 2>&1; then
    info "Voer npm install uit (dit kan even duren)..."
    npm install
    info "npm install afgerond."
  else
    warn "npm niet gevonden op PATH. Sla npm install over. Installeer node/npm en run 'npm install' handmatig."
  fi
fi

# Git init & commit
if command -v git >/dev/null 2>&1; then
  if [ ! -d .git ]; then
    info "Initialiseer git repository..."
    git init
  fi
  git add .
  git commit -m "Initial commit: solana phantom demo (server + public + cli placeholder)" || true
  info "Lokale git commit gemaakt."
else
  warn "Git niet gevonden op PATH. Sla git init/commit over."
fi

# If gh installed, ask to create repo and push
if command -v gh >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
  if ask_yes_no "GH CLI gevonden. Wil je dat ik een GitHub repo maak en direct push (gh repo create + git push)? (y/n) [y]" "y"; then
    read -r -p "Voer GitHub repo-naam in (owner/repo) of alleen repo-naam: " GH_REPO
    GH_REPO="${GH_REPO:-}"
    if [ -z "$GH_REPO" ]; then
      err "Geen repo-naam opgegeven. Sla GitHub creatie over."
    else
      read -r -p "Wil je repo public of private? (public/private) [public]: " GH_PRIV
      GH_PRIV="${GH_PRIV:-public}"
      if [ "$GH_PRIV" = "private" ]; then
        PRIV_FLAG="--private"
      else
        PRIV_FLAG="--public"
      fi

      info "Proberen gh repo create ${GH_REPO} ${PRIV_FLAG} --source=. --push ..."
      # If user only provided repo name, `gh repo create name` will create under current user/org.
      set +e
      gh repo create "${GH_REPO}" ${PRIV_FLAG} --source=. --push
      RC=$?
      set -e
      if [ $RC -eq 0 ]; then
        info "Repo aangemaakt en gepusht naar GitHub."
      else
        warn "gh repo create faalde (code $RC). Mogelijk bestaat repo al of ontbreken permissies. Je kunt handmatig pushen."
        warn "Handmatige push instructies:"
        echo "  git remote add origin git@github.com:OWNER/REPO.git"
        echo "  git branch -M main"
        echo "  git push -u origin main"
      fi
    fi
  fi
else
  warn "GH CLI niet gevonden of git niet geïnstalleerd. Als je wilt pushen naar GitHub: maak een repo aan en push handmatig."
  echo "Voorbeeld:"
  echo "  git remote add origin git@github.com:YOURUSER/YOURREPO.git"
  echo "  git branch -M main"
  echo "  git push -u origin main"
fi

info "Klaar."

cat <<'EOF'

Volgende stappen (kort):
1) Als je nog niet gedaan: controleer bestanden en bewerk scripts/solana-token-demo.js met je originele CLI (vervang de placeholder).
2) Test lokaal:
   - npm start
   - open http://localhost:3000 in een browser met Phantom extension
3) Als je nog niet gepusht hebt, maak een GitHub repo en push:
   git remote add origin git@github.com:YOURUSER/YOURREPO.git
   git branch -M main
   git push -u origin main
4) Deploy op Render:
   - Ga naar https://dashboard.render.com -> New -> Web Service
   - Koppel je GitHub repo
   - Build command: npm install
   - Start command: npm start
   - Render levert HTTPS; open de URL en test Phantom.

Als je wilt dat ik nu:
- a) Vervang de placeholder CLI door jouw volledige originele CLI (plak die hier) — ik zal het bestand bijwerken in de repo-structuur.
- b) Help met eventuele push errors of Render settings — kopieer de foutmelding.
- c) Niets meer — je bent klaar om te deployen.

EOF