require("dotenv").config();
const {
  Client,
  GatewayIntentBits,
  REST,
  Routes,
  SlashCommandBuilder,
  PermissionFlagsBits,
} = require("discord.js");
const { google } = require("googleapis");

// -------------------- Google Docs --------------------
function getGoogleAuth() {
  if (!process.env.GOOGLE_CREDS_JSON) {
    throw new Error("Lipseste GOOGLE_CREDS_JSON in env.");
  }
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

async function appendToDoc(text) {
  const documentId = process.env.GOOGLE_DOC_ID;
  if (!documentId) throw new Error("Lipseste GOOGLE_DOC_ID in env.");

  const auth = getGoogleAuth();
  const docs = google.docs({ version: "v1", auth });

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

function nowIso() {
  return new Date().toISOString();
}

function tagUser(u) {
  return `${u.tag} (${u.id})`;
}

// -------------------- Discord Bot --------------------
const client = new Client({
  intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMembers],
});

client.once("ready", () => {
  console.log(`✅ Bot online: ${client.user.tag}`);
});

// -------------------- Slash Commands Registration --------------------
async function registerCommands() {
  const token = process.env.DISCORD_TOKEN;
  const appId = process.env.DISCORD_CLIENT_ID;
  const guildId = process.env.GUILD_ID; // recomandat pentru aparitie instant

  if (!token || !appId) {
    console.log("ℹ️ Lipseste DISCORD_TOKEN sau DISCORD_CLIENT_ID, sar peste register.");
    return;
  }

  const commands = [
    new SlashCommandBuilder().setName("info").setDescription("Info bot"),
    new SlashCommandBuilder()
      .setName("procesare")
      .setDescription("Fa o procesare")
      .addStringOption((o) =>
        o.setName("nume_prenume").setDescription("Nume Prenume").setRequired(true)
      )
      .addStringOption((o) => o.setName("cnp").setDescription("CNP").setRequired(true)),

    // certificate
    new SlashCommandBuilder()
      .setName("adauga-certificat")
      .setDescription("Adauga certificat unui membru")
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addStringOption((o) =>
        o.setName("certificat").setDescription("Nume certificat").setRequired(true)
      ),

    new SlashCommandBuilder()
      .setName("scoate-certificat")
      .setDescription("Scoate certificat unui membru")
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addStringOption((o) =>
        o.setName("certificat").setDescription("Nume certificat").setRequired(true)
      ),

    // cerere insigna
    new SlashCommandBuilder()
      .setName("cerere-insigna")
      .setDescription("Cerere pentru o insigna")
      .addStringOption((o) =>
        o.setName("insigna").setDescription("Numele insignei").setRequired(true)
      )
      .addStringOption((o) =>
        o.setName("detalii").setDescription("Detalii / motiv").setRequired(false)
      ),

    // concediu
    new SlashCommandBuilder()
      .setName("concediu-adauga")
      .setDescription("Adauga concediu unui membru")
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addStringOption((o) =>
        o.setName("de_la").setDescription("De la (ex: 2026-02-06)").setRequired(true)
      )
      .addStringOption((o) =>
        o.setName("pana_la").setDescription("Pana la (ex: 2026-02-10)").setRequired(true)
      )
      .addStringOption((o) => o.setName("motiv").setDescription("Motiv").setRequired(false)),

    new SlashCommandBuilder()
      .setName("concediu-anuleaza")
      .setDescription("Anuleaza concediu unui membru")
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addStringOption((o) =>
        o.setName("motiv").setDescription("Motiv anulare").setRequired(false)
      ),

    // incheie saptamana
    new SlashCommandBuilder()
      .setName("incheie-saptamana")
      .setDescription("Incheie saptamana de pontaj si trece-le pe docs")
      .addStringOption((o) =>
        o.setName("nota").setDescription("Nota optionala").setRequired(false)
      ),

    // lockdown
    new SlashCommandBuilder()
      .setName("lockdown-deschide")
      .setDescription("Deschide un canal (ridica lockdown)")
      .setDefaultMemberPermissions(PermissionFlagsBits.ManageChannels),

    new SlashCommandBuilder()
      .setName("lockdown-inchide")
      .setDescription("Inchide un canal (lockdown)")
      .setDefaultMemberPermissions(PermissionFlagsBits.ManageChannels),

    // pontaj rutiera
    new SlashCommandBuilder()
      .setName("pontaj-rutiera")
      .setDescription("Deschide/Inchide pontajul de rutiera")
      .addStringOption((o) =>
        o
          .setName("actiune")
          .setDescription("Ce vrei sa faci?")
          .setRequired(true)
          .addChoices(
            { name: "deschide", value: "deschide" },
            { name: "inchide", value: "inchide" }
          )
      )
      .addStringOption((o) =>
        o.setName("nota").setDescription("Nota optionala").setRequired(false)
      ),

    // promovari / retrogradari
    new SlashCommandBuilder()
      .setName("promoveaza-membru")
      .setDescription("Promoveaza membru (adauga rol)")
      .setDefaultMemberPermissions(PermissionFlagsBits.ManageRoles)
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addRoleOption((o) => o.setName("rol").setDescription("Rol nou").setRequired(true))
      .addStringOption((o) => o.setName("motiv").setDescription("Motiv").setRequired(false)),

    new SlashCommandBuilder()
      .setName("retrogradeaza-membru")
      .setDescription("Retrogradeaza membru (scoate rol)")
      .setDefaultMemberPermissions(PermissionFlagsBits.ManageRoles)
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addRoleOption((o) => o.setName("rol").setDescription("Rol de scos").setRequired(true))
      .addStringOption((o) => o.setName("motiv").setDescription("Motiv").setRequired(false)),

    new SlashCommandBuilder()
      .setName("scoate-membru")
      .setDescription("Scoate membru (kick) sau scoate rolul ales")
      .setDefaultMemberPermissions(PermissionFlagsBits.KickMembers)
      .addUserOption((o) => o.setName("membru").setDescription("Membru").setRequired(true))
      .addStringOption((o) =>
        o
          .setName("actiune")
          .setDescription("Ce faci cu el?")
          .setRequired(true)
          .addChoices(
            { name: "kick", value: "kick" },
            { name: "scoate-rol", value: "scoate-rol" }
          )
      )
      .addRoleOption((o) =>
        o.setName("rol").setDescription("Daca actiunea e scoate-rol").setRequired(false)
      )
      .addStringOption((o) => o.setName("motiv").setDescription("Motiv").setRequired(false)),

    // uptime
    new SlashCommandBuilder()
      .setName("uptime")
      .setDescription("Vezi de cat timp e botul pornit"),
  ].map((c) => c.toJSON());

  const rest = new REST({ version: "10" }).setToken(token);

  if (guildId) {
    await rest.put(Routes.applicationGuildCommands(appId, guildId), { body: commands });
    console.log("✅ Slash commands registered (GUILD).");
  } else {
    await rest.put(Routes.applicationCommands(appId), { body: commands });
    console.log("✅ Slash commands registered (GLOBAL).");
  }
}

// -------------------- Handlers --------------------
client.on("interactionCreate", async (interaction) => {
  if (!interaction.isChatInputCommand()) return;

  const user = interaction.user;
  const guild = interaction.guild;
  const channel = interaction.channel;

  try {
    // /info
    if (interaction.commandName === "info") {
      return interaction.reply({ content: "Bot Politie e online ✅", ephemeral: true });
    }

    // /uptime
    if (interaction.commandName === "uptime") {
      const sec = Math.floor(process.uptime());
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      return interaction.reply({
        content: `⏱️ Uptime: ${h}h ${m}m ${s}s`,
        ephemeral: true,
      });
    }

    // /procesare
    if (interaction.commandName === "procesare") {
      const nume = interaction.options.getString("nume_prenume", true);
      const cnp = interaction.options.getString("cnp", true);

      const line = `PROCESARE | ${nowIso()} | by ${tagUser(user)} | ${nume} | ${cnp} | guild=${guild?.id}`;
      await appendToDoc(line);

      return interaction.reply({ content: "✅ Procesare adaugata in Google Docs.", ephemeral: true });
    }

    // certificate
    if (interaction.commandName === "adauga-certificat") {
      const membru = interaction.options.getUser("membru", true);
      const cert = interaction.options.getString("certificat", true);

      await appendToDoc(
        `CERTIFICAT_ADD | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | certificat=${cert}`
      );
      return interaction.reply({ content: "✅ Certificat adaugat (log in Docs).", ephemeral: true });
    }

    if (interaction.commandName === "scoate-certificat") {
      const membru = interaction.options.getUser("membru", true);
      const cert = interaction.options.getString("certificat", true);

      await appendToDoc(
        `CERTIFICAT_REMOVE | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | certificat=${cert}`
      );
      return interaction.reply({ content: "✅ Certificat scos (log in Docs).", ephemeral: true });
    }

    // cerere insigna
    if (interaction.commandName === "cerere-insigna") {
      const insigna = interaction.options.getString("insigna", true);
      const detalii = interaction.options.getString("detalii", false) || "-";

      await appendToDoc(
        `CERERE_INSIGNA | ${nowIso()} | by ${tagUser(user)} | insigna=${insigna} | detalii=${detalii}`
      );
      return interaction.reply({ content: "✅ Cerere insigna trimisa (log in Docs).", ephemeral: true });
    }

    // concediu
    if (interaction.commandName === "concediu-adauga") {
      const membru = interaction.options.getUser("membru", true);
      const deLa = interaction.options.getString("de_la", true);
      const panaLa = interaction.options.getString("pana_la", true);
      const motiv = interaction.options.getString("motiv", false) || "-";

      await appendToDoc(
        `CONCEDIU_ADD | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | de_la=${deLa} | pana_la=${panaLa} | motiv=${motiv}`
      );
      return interaction.reply({ content: "✅ Concediu adaugat (log in Docs).", ephemeral: true });
    }

    if (interaction.commandName === "concediu-anuleaza") {
      const membru = interaction.options.getUser("membru", true);
      const motiv = interaction.options.getString("motiv", false) || "-";

      await appendToDoc(
        `CONCEDIU_CANCEL | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | motiv=${motiv}`
      );
      return interaction.reply({ content: "✅ Concediu anulat (log in Docs).", ephemeral: true });
    }

    // incheie saptamana
    if (interaction.commandName === "incheie-saptamana") {
      const nota = interaction.options.getString("nota", false) || "-";
      await appendToDoc(
        `INCHEIE_SAPTAMANA | ${nowIso()} | by ${tagUser(user)} | guild=${guild?.id} | nota=${nota}`
      );
      return interaction.reply({ content: "✅ Saptamana incheiata (log in Docs).", ephemeral: true });
    }

    // pontaj rutiera
    if (interaction.commandName === "pontaj-rutiera") {
      const actiune = interaction.options.getString("actiune", true);
      const nota = interaction.options.getString("nota", false) || "-";

      await appendToDoc(
        `PONTAJ_RUTIERA_${actiune.toUpperCase()} | ${nowIso()} | by ${tagUser(user)} | guild=${guild?.id} | nota=${nota}`
      );
      return interaction.reply({
        content: `✅ Pontaj rutiera: **${actiune}** (log in Docs).`,
        ephemeral: true,
      });
    }

    // lockdown (pe canalul curent)
    if (interaction.commandName === "lockdown-inchide") {
      if (!guild || !channel?.isTextBased()) {
        return interaction.reply({ content: "❌ Comanda merge doar intr-un server, pe un canal text.", ephemeral: true });
      }

      await channel.permissionOverwrites.edit(guild.roles.everyone, { SendMessages: false });

      await appendToDoc(
        `LOCKDOWN_INCHIDE | ${nowIso()} | by ${tagUser(user)} | guild=${guild.id} | channel=${channel.id}`
      );

      return interaction.reply({ content: "🔒 Lockdown ACTIV pe canalul curent.", ephemeral: true });
    }

    if (interaction.commandName === "lockdown-deschide") {
      if (!guild || !channel?.isTextBased()) {
        return interaction.reply({ content: "❌ Comanda merge doar intr-un server, pe un canal text.", ephemeral: true });
      }

      // null = revine la permisiunile normale ale serverului
      await channel.permissionOverwrites.edit(guild.roles.everyone, { SendMessages: null });

      await appendToDoc(
        `LOCKDOWN_DESCHIDE | ${nowIso()} | by ${tagUser(user)} | guild=${guild.id} | channel=${channel.id}`
      );

      return interaction.reply({ content: "🔓 Lockdown RIDICAT pe canalul curent.", ephemeral: true });
    }

    // promovari / retrogradari
    if (interaction.commandName === "promoveaza-membru") {
      if (!guild) return interaction.reply({ content: "❌ Doar in server.", ephemeral: true });

      const membru = interaction.options.getUser("membru", true);
      const rol = interaction.options.getRole("rol", true);
      const motiv = interaction.options.getString("motiv", false) || "-";

      const member = await guild.members.fetch(membru.id);
      await member.roles.add(rol);

      await appendToDoc(
        `PROMOVEAZA | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | rol=${rol.name} (${rol.id}) | motiv=${motiv}`
      );

      return interaction.reply({ content: `✅ ${membru} a primit rolul **${rol.name}**.`, ephemeral: true });
    }

    if (interaction.commandName === "retrogradeaza-membru") {
      if (!guild) return interaction.reply({ content: "❌ Doar in server.", ephemeral: true });

      const membru = interaction.options.getUser("membru", true);
      const rol = interaction.options.getRole("rol", true);
      const motiv = interaction.options.getString("motiv", false) || "-";

      const member = await guild.members.fetch(membru.id);
      await member.roles.remove(rol);

      await appendToDoc(
        `RETROGRADEAZA | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | rol=${rol.name} (${rol.id}) | motiv=${motiv}`
      );

      return interaction.reply({ content: `✅ ${membru} i-a fost scos rolul **${rol.name}**.`, ephemeral: true });
    }

    if (interaction.commandName === "scoate-membru") {
      if (!guild) return interaction.reply({ content: "❌ Doar in server.", ephemeral: true });

      const membru = interaction.options.getUser("membru", true);
      const actiune = interaction.options.getString("actiune", true);
      const rol = interaction.options.getRole("rol", false);
      const motiv = interaction.options.getString("motiv", false) || "-";

      const member = await guild.members.fetch(membru.id);

      if (actiune === "kick") {
        await member.kick(motiv);
        await appendToDoc(
          `SCOATE_MEMBRU_KICK | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | motiv=${motiv}`
        );
        return interaction.reply({ content: `✅ ${membru} a fost dat afara (kick).`, ephemeral: true });
      }

      // scoate-rol
      if (!rol) {
        return interaction.reply({ content: "❌ Pentru scoate-rol trebuie sa alegi si un rol.", ephemeral: true });
      }
      await member.roles.remove(rol);
      await appendToDoc(
        `SCOATE_MEMBRU_ROL | ${nowIso()} | by ${tagUser(user)} | membru=${tagUser(membru)} | rol=${rol.name} (${rol.id}) | motiv=${motiv}`
      );
      return interaction.reply({ content: `✅ I-am scos rolul **${rol.name}** lui ${membru}.`, ephemeral: true });
    }

    return interaction.reply({ content: "Comanda necunoscuta.", ephemeral: true });
  } catch (e) {
    console.error(e);
    const msg = "❌ Eroare la comanda. Verifica Logs in Render.";
    if (interaction.replied || interaction.deferred) return interaction.followUp({ content: msg, ephemeral: true });
    return interaction.reply({ content: msg, ephemeral: true });
  }
});

// -------------------- Start --------------------
(async () => {
  await registerCommands();
  await client.login(process.env.DISCORD_TOKEN);
})();
