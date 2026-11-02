// Reads the past messages of WhatsApp channels and groups once, into data/history, for the same audit as the Telegram
// channels, then exits. It only reads: it never sends, reacts or follows anything.
//
// WhatsApp doesn't allow unofficial clients, so log in with a SPARE number, never your main one: on that phone follow the
// channel (or join the group) first, then scan the QR code this prints (WhatsApp > Linked devices > Link a device).
//
//   node history.js "TAMIL SPORTS CENTER"
//   node history.js --list     every channel and group the number has
//   node history.js --check    only proves it starts: exits at the QR code, no login
const fs = require("fs");
const path = require("path");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth } = require("whatsapp-web.js");

const CHROME = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const OUT = path.resolve(__dirname, "..", "..", "data", "history");
const MOST = 5000; // messages per chat
const args = process.argv.slice(2);
const check = args.includes("--check");
const list = args.includes("--list");
const wanted = args.filter((a) => !a.startsWith("--")).map((a) => a.toLowerCase());
if (check) process.on("unhandledRejection", () => process.exit(0)); // closing at the QR code breaks the login's pending calls

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: path.join(__dirname, ".wwebjs_auth") }), // the login stays here: keep it private
    puppeteer: { executablePath: CHROME, headless: true },
});

function slug(name) {
    return "whatsapp_" + name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

function localTime(seconds) {
    const d = new Date(seconds * 1000);
    const two = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

// The same fields as the Telegram histories, plus who wrote it (a group has many writers).
async function record(msg, folder) {
    const item = {
        id: msg.id._serialized, time: localTime(msg.timestamp), kind: msg.type, text: msg.body || "",
        author: msg.author || "", forwarded: Boolean(msg.isForwarded), photo: msg.type === "image", media: msg.hasMedia ? msg.type : "",
        reply_to: msg.hasQuotedMsg ? "yes" : "", edited: false, duration: msg.duration || 0,
    };
    if (msg.type === "image") {
        try {
            const media = await msg.downloadMedia();
            if (media) {
                fs.mkdirSync(folder, { recursive: true });
                fs.writeFileSync(path.join(folder, `${msg.timestamp}_${msg.id.id}.jpg`), Buffer.from(media.data, "base64"));
            }
        } catch (err) {
            item.media_error = String(err).slice(0, 120);
        }
    }
    return item;
}

async function save(chat) {
    const messages = await chat.fetchMessages({ limit: MOST });
    const name = slug(chat.name);
    const items = [];
    for (const msg of messages) items.push(await record(msg, path.join(OUT, name)));
    fs.writeFileSync(path.join(OUT, `${name}.json`), JSON.stringify(items, null, 1), "utf8");
    console.log(`${chat.name}: ${items.length} messages, ${items[0]?.time || "-"} to ${items.at(-1)?.time || "-"} -> data/history/${name}.json`);
}

client.on("qr", (qr) => {
    if (check) {
        console.log(`It starts: WhatsApp Web gave a QR code (${qr.length} characters). Not logging in.`);
        client.destroy().finally(() => process.exit(0));
        return;
    }
    console.log("Scan this with the SPARE number: WhatsApp > Linked devices > Link a device");
    qrcode.generate(qr, { small: true });
});
client.on("auth_failure", (reason) => {
    console.error("Login failed:", reason);
    process.exit(1);
});
client.on("ready", async () => {
    try {
        const chats = [...await client.getChats(), ...await client.getChannels()];
        const seen = new Map(chats.filter((c) => c.isGroup || c.isChannel).map((c) => [c.id._serialized, c]));
        const known = [...seen.values()];
        if (list || !wanted.length) {
            for (const c of known) console.log(`${c.isChannel ? "channel" : "group  "} ${c.name}`);
        }
        for (const name of wanted) {
            const found = known.filter((c) => c.name.toLowerCase().includes(name));
            if (!found.length) console.log(`Not found: ${name}. Follow or join it on the phone first; --list shows what this number has.`);
            for (const chat of found) await save(chat);
        }
    } catch (err) {
        console.error(err);
        process.exitCode = 1;
    }
    await client.destroy();
    process.exit();
});
client.initialize();
