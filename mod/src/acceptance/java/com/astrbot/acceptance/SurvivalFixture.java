package com.astrbot.acceptance;

import com.google.gson.JsonObject;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Set;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.Registries;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.Difficulty;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.GameRules;
import net.minecraft.world.level.GameType;
import net.minecraft.world.level.LevelSettings;
import net.minecraft.world.level.WorldDataConfiguration;
import net.minecraft.world.level.block.BedBlock;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.CropBlock;
import net.minecraft.world.level.block.state.properties.BedPart;
import net.minecraft.world.level.levelgen.WorldOptions;
import net.minecraft.world.level.levelgen.presets.WorldPresets;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.common.NeoForge;

/** Separate TEST jar, never included in the released bridge. Refuses every multiplayer world. */
@Mod(value="mc_survival_fixture",dist=Dist.CLIENT)
public final class SurvivalFixture {
    static final String WORLD="local-scheduler-20261102".equals(System.getProperty("MinecraftBot.acceptance",""))?"Local-Scheduler-20261102":"Local-Survival-20261101";
    private final boolean enabled=Set.of("survival-20261101","local-scheduler-20261102").contains(System.getProperty("MinecraftBot.acceptance",""));
    private final Path output=Path.of(System.getProperty("mc.acceptance.dir","."));
    private boolean opened;
    private volatile boolean ready;
    private long next;
    private String lastPhase="";
    public SurvivalFixture(IEventBus bus,ModContainer container){NeoForge.EVENT_BUS.register(this);}
    @SubscribeEvent public void tick(ClientTickEvent.Post event) {
        if(!enabled)return;
        Minecraft mc=Minecraft.getInstance();long now=System.currentTimeMillis();
        if(ready&&"local-scheduler-20261102".equals(System.getProperty("MinecraftBot.acceptance",""))){LocalSchedulerFixture.tick(mc,output);return;}
        if(!opened && mc.screen instanceof TitleScreen && mc.player==null && now>next) {
            opened=true;
            if(Files.isDirectory(mc.gameDirectory.toPath().resolve("saves").resolve(WORLD))) {
                mc.createWorldOpenFlows().openWorld(WORLD,()->write("world-open-failed",false));
            } else {
                GameRules rules=new GameRules();
                rules.getRule(GameRules.RULE_DAYLIGHT).set(false,null);
                rules.getRule(GameRules.RULE_DOMOBSPAWNING).set(false,null);
                rules.getRule(GameRules.RULE_SPAWN_CHUNK_RADIUS).set(0,null);
                LevelSettings settings=new LevelSettings(WORLD,GameType.SURVIVAL,false,Difficulty.PEACEFUL,false,rules,WorldDataConfiguration.DEFAULT);
                mc.createWorldOpenFlows().createFreshLevel(WORLD,settings,new WorldOptions(20261101L,false,false),
                    access->access.registryOrThrow(Registries.WORLD_PRESET).getHolderOrThrow(WorldPresets.FLAT).value().createWorldDimensions(),mc.screen);
            }
        }
        if(opened&&enabled&&mc.player==null&&mc.screen instanceof net.minecraft.client.gui.screens.BackupConfirmScreen){for(var child:mc.screen.children())if(child instanceof net.minecraft.client.gui.components.Button button&&button.getMessage().getContents() instanceof net.minecraft.network.chat.contents.TranslatableContents contents&&contents.getKey().equals("selectWorld.backupJoinSkipButton")){button.onPress();return;}}
        if(now<next||mc.player==null||mc.getSingleplayerServer()==null||!WORLD.equals(mc.getSingleplayerServer().getWorldData().getLevelName()))return;
        next=now+1000;
        var server=mc.getSingleplayerServer();var uuid=mc.player.getUUID();
        server.execute(()->{
            ServerPlayer player=server.getPlayerList().getPlayer(uuid);
            if(player==null)return;
            if(!ready){prepare(player);ready=true;write("fixture-ready",true);}
            try {
                Path phase=output.resolve("phase.txt");String value=Files.exists(phase)?Files.readString(phase).strip():"";
                if(!value.equals(lastPhase)) {
                    lastPhase=value;
                    if(value.equals("night")){player.serverLevel().setDayTime(13000);write("night",true);}
                    if(value.equals("day")){player.serverLevel().setDayTime(6000);write("day",true);}
                    if(value.equals("hungry")){player.getFoodData().setFoodLevel(14);player.getFoodData().setSaturation(0);write("hungry",true);}
                    if(value.equals("no_tables")) {
                        player.serverLevel().setBlock(new BlockPos(-2,65,2),Blocks.AIR.defaultBlockState(),3);
                        player.serverLevel().setBlock(new BlockPos(2,65,2),Blocks.AIR.defaultBlockState(),3);
                        write("no_tables",true);
                    }
                }
            } catch(Exception ex){write("phase-error",false);}
        });
    }
    private void prepare(ServerPlayer player) {
        var level=player.serverLevel();
        player.server.setDifficulty(Difficulty.NORMAL,true);
        for(BlockPos pos:BlockPos.betweenClosed(-16,64,-16,24,72,16))
            level.setBlock(pos,pos.getY()==64?Blocks.GRASS_BLOCK.defaultBlockState():Blocks.AIR.defaultBlockState(),2);
        for(int y=65;y<=68;y++)level.setBlock(new BlockPos(6,y,0),Blocks.OAK_LOG.defaultBlockState(),3);
        for(int y=65;y<=68;y++)level.setBlock(new BlockPos(10,y,5),Blocks.OAK_LOG.defaultBlockState(),3);
        for(BlockPos pos:BlockPos.betweenClosed(8,68,3,12,69,7))if(!level.getBlockState(pos).is(Blocks.OAK_LOG))level.setBlock(pos,Blocks.OAK_LEAVES.defaultBlockState(),3);
        for(BlockPos pos:BlockPos.betweenClosed(4,68,-2,8,69,2))if(!level.getBlockState(pos).is(Blocks.OAK_LOG))level.setBlock(pos,Blocks.OAK_LEAVES.defaultBlockState(),3);
        level.setBlock(new BlockPos(8,65,11),Blocks.COAL_ORE.defaultBlockState(),3);
        level.setBlock(new BlockPos(10,65,8),Blocks.IRON_ORE.defaultBlockState(),3);
        for(int z=4;z<=9;z++)for(int x=8;x<=9;x++)level.setBlock(new BlockPos(x,65,z),Blocks.STONE.defaultBlockState(),3);
        level.setBlock(new BlockPos(-2,65,2),Blocks.CRAFTING_TABLE.defaultBlockState(),3);
        level.setBlock(new BlockPos(-2,65,-2),Blocks.RED_BED.defaultBlockState().setValue(BedBlock.FACING,Direction.NORTH).setValue(BedBlock.PART,BedPart.FOOT),3);
        level.setBlock(new BlockPos(-2,65,-3),Blocks.RED_BED.defaultBlockState().setValue(BedBlock.FACING,Direction.NORTH).setValue(BedBlock.PART,BedPart.HEAD),3);
        for(int x=3;x<=5;x++) {
            level.setBlock(new BlockPos(x,64,-5),Blocks.FARMLAND.defaultBlockState(),3);
            level.setBlock(new BlockPos(x,65,-5),Blocks.WHEAT.defaultBlockState().setValue(CropBlock.AGE,7),3);
        }
        player.getInventory().clearContent();
        player.getInventory().setItem(0,new ItemStack(Items.DIAMOND_SWORD));
        player.getInventory().setItem(1,new ItemStack(Items.STONE_AXE));
        player.getInventory().setItem(2,new ItemStack(Items.IRON_PICKAXE));
        player.getInventory().setItem(9,new ItemStack(Items.OAK_LOG,4));
        player.getInventory().setItem(20,new ItemStack(Items.BREAD,4));
        player.getInventory().setItem(21,new ItemStack(Items.IRON_CHESTPLATE));
        player.getInventory().setItem(22,new ItemStack(Items.WHEAT_SEEDS,16));
        player.getInventory().setItem(23,new ItemStack(Items.WHEAT,3));
        player.getInventory().setItem(24,new ItemStack(Items.CRAFTING_TABLE));
        player.getInventory().selected=0;player.setGameMode(GameType.SURVIVAL);
        player.getFoodData().setFoodLevel(14);player.getFoodData().setSaturation(0);player.setHealth(player.getMaxHealth());
        player.teleportTo(level,0.5,65,0.5,0,0);level.setDayTime(6000);
        Set<String> recipes=Set.of("oak_planks","stick","crafting_table","bread","wooden_pickaxe","wooden_axe","stone_pickaxe","stone_axe");
        player.awardRecipes(serverRecipes(player,recipes));
        player.inventoryMenu.broadcastChanges();
    }
    private java.util.Collection<net.minecraft.world.item.crafting.RecipeHolder<?>> serverRecipes(ServerPlayer p,Set<String> ids) {
        return p.server.getRecipeManager().getRecipes().stream().filter(r->r.id().getNamespace().equals("minecraft")&&ids.contains(r.id().getPath())).toList();
    }
    private void write(String phase,boolean ok) {
        try{Files.createDirectories(output);JsonObject j=new JsonObject();j.addProperty("world",WORLD);j.addProperty("phase",phase);j.addProperty("ok",ok);Files.writeString(output.resolve(phase+".json"),j.toString());}
        catch(Exception ignored){}
    }
}
