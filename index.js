require("dotenv").config();
const { Client, GatewayIntentBits, REST, Routes, SlashCommandBuilder } = require("discord.js");
const { google } = require("googleapis");

function getGoogleAuth() {
  // In cloud: cheia vine din env ca JSON text
  if (process.env.GOOGLE_CREDS_JSON) {
    const creds = JSON.parse(process.env.GOOGLE_CREDS_JSON);
    return new google.auth.JWT({
      email: creds.client_email,
      key: creds.private_key,
      scopes: [
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/drive",
      ],
    });
  }
  throw new Error("Lipseste GOOGLE_CREDS_JSON in env.");
}

async function appendToDoc(text) {
  const auth = getGoogleAuth();
  const docs = google.docs({ version: "v1", auth });

  const documentId = process.env.GOOGLE_DOC_ID;
  if (!documentId) throw new Error("Lipseste GOOGLE_DOC_ID in env.");

  const doc = await docs.documents.get({ documentId });
  const endIndex = doc.data.body.content[doc.data.body.content.length - 1].endIndex;

  await docs.documents.batchUpdate({
    documentId,
    requestBody: {
      requests: [
        {
          insertText: {
            location: { index: endIndex - 1 },
            text: text.endsWith("\n") ? text : text + "\n",
          },
        },
      ],
    },
  });
}

// -------- Discord bot ----------
const client = new Client({ intents: [GatewayIntentBits.Guilds] });

client.once("ready", () => {
  console.log(`✅ Bot online: ${client.user.tag}`);
});

// Comenzi
client.on("interactionCreate", async (interaction) => {
  if (!interaction.isChatInputCommand()) return;

  try {
    if (interaction.commandName === "info") {
      return interaction.reply({ content: "Bot Politie e online ✅", ephemeral: true });
    }

    if (interaction.commandName === "procesare") {
      const nume = interaction.options.getString("nume_prenume", true);
      const cnp = interaction.options.getString("cnp", true);

      const line = `PROCESARE | ${new Date().toISOString()} | ${interaction.user.tag} | ${nume} | ${cnp}`;
      await appendToDoc(line);

      return interaction.reply({ content: "✅ Procesare adaugata in Google Docs.", ephemeral: true });
    }

    return interaction.reply({ content: "Comanda necunoscuta.", ephemeral: true });
  } catch (e) {
    console.error(e);
    if (interaction.replied || interaction.deferred) {
      return interaction.followUp({ content: "Eroare la comanda.", ephemeral: true });
    }
    return interaction.reply({ content: "Eroare la comanda.", ephemeral: true });
  }
});

// Auto-register commands (o singura data la pornire)
async function registerCommands() {
  const token = process.env.DISCORD_TOKEN;
  const appId = process.env.DISCORD_CLIENT_ID;
  const guildId = process.env.GUILD_ID; // optional, pt test rapid

  if (!token || !appId) {
    console.log("ℹ️ Lipseste DISCORD_CLIENT_ID sau DISCORD_TOKEN, sar peste register.");
    return;
  }

  const commands = [
    new SlashCommandBuilder().setName("info").setDescription("Info"),
    new SlashCommandBuilder()
      .setName("procesare")
      .setDescription("Fa o procesare")
      .addStringOption(o => o.setName("nume_prenume").setDescription("Nume Prenume").setRequired(true))
      .addStringOption(o => o.setName("cnp").setDescription("CNP").setRequired(true)),
  ].map(c => c.toJSON());

  const rest = new REST({ version: "10" }).setToken(token);

  if (guildId) {
    await rest.put(Routes.applicationGuildCommands(appId, guildId), { body: commands });
    console.log("✅ Slash commands registered (GUILD).");
  } else {
    await rest.put(Routes.applicationCommands(appId), { body: commands });
    console.log("✅ Slash commands registered (GLOBAL).");
  }
}

(async () => {
  await registerCommands();
  await client.login(process.env.DISCORD_TOKEN);
})();
