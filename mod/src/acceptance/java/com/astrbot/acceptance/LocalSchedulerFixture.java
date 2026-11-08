package com.astrbot.acceptance;

import com.google.gson.*;
import java.lang.reflect.*;
import java.net.http.WebSocket;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.Blocks;

/** TEST SOURCE ONLY. Executes against the real adapter in a dedicated singleplayer world.
 * The bridge transport is an in-process recorder; no cloud/QQ/socket is contacted. */
final class LocalSchedulerFixture {
    private static final Map<String,JsonObject> replies=new HashMap<>();
    private static final List<JsonObject> events=new ArrayList<>();
    private static final JsonArray cases=new JsonArray();
    private static Object bridge,scheduler;
    private static Method handle,status,taskStatus,busy;
    private static int phase;
    private static long next,started;
    private static int logsAtStop;
    private static Path output;
    private static final String A="11111111111111111111111111111111",B="22222222222222222222222222222222",C="33333333333333333333333333333333",D="44444444444444444444444444444444";
    private static final String E="55555555555555555555555555555555",F="66666666666666666666666666666666",G="77777777777777777777777777777777";
    private static final String H="88888888888888888888888888888888",I="99999999999999999999999999999999",J="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",K="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",L="cccccccccccccccccccccccccccccccc",M="dddddddddddddddddddddddddddddddd",N="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    private static JsonObject quarryResult;
    private static int quarryTrial;
    private static String quarryTask=L;
    private static long deathAt;
    private static boolean ended;
    static void tick(Minecraft mc,Path directory){
        if(ended||!"local-scheduler-20261102".equals(System.getProperty("MinecraftBot.acceptance",""))||mc.player==null||mc.getSingleplayerServer()==null
                ||!mc.getSingleplayerServer().getWorldData().getLevelName().equals("Codex-LocalScheduler-20261102"))return;
        output=directory;long now=System.currentTimeMillis();if(now<next)return;next=now+100;
        try{
            if(phase==0){
                Class<?> type=Class.forName("com.astrbot.bridge.BridgeClient");bridge=type.getMethod("get").invoke(null);
                Field field=type.getDeclaredField("localTasks");field.setAccessible(true);scheduler=field.get(bridge);
                handle=type.getDeclaredMethod("handleAction",JsonObject.class);handle.setAccessible(true);
                status=type.getDeclaredMethod("status",net.minecraft.client.player.LocalPlayer.class);status.setAccessible(true);
                taskStatus=scheduler.getClass().getDeclaredMethod("status",String.class);taskStatus.setAccessible(true);
                busy=scheduler.getClass().getDeclaredMethod("busy");busy.setAccessible(true);
                WebSocket[] socket=new WebSocket[1];
                socket[0]=(WebSocket)Proxy.newProxyInstance(WebSocket.class.getClassLoader(),new Class<?>[]{WebSocket.class},(proxy,method,args)->{
                    if(method.getName().equals("sendText")){
                        JsonObject message=JsonParser.parseString(args[0].toString()).getAsJsonObject();
                        if(message.has("id"))replies.put(message.get("id").getAsString(),message);
                        if(message.has("event")&&message.get("event").getAsString().equals("task_update"))events.add(message.getAsJsonObject("data"));
                    }
                    if(method.getReturnType()==CompletableFuture.class)return CompletableFuture.completedFuture(socket[0]);
                    if(method.getReturnType()==boolean.class)return false;
                    if(method.getReturnType()==String.class)return "local-acceptance-recorder";
                    return null;
                });
                for(String name:List.of("started","autoConnect","socket")){
                    field=type.getDeclaredField(name);field.setAccessible(true);field.set(bridge,name.equals("started")?true:name.equals("autoConnect")?false:socket[0]);
                }
                started=now;phase=1;next=now+2000;
            }else if(phase==1){
                if(mc.screen!=null||mc.isPaused()){
                    if(now-started>15000)throw new IllegalStateException("fixture still has GUI: "+(mc.screen==null?"paused":mc.screen.getClass().getSimpleName()));
                    if(mc.player.containerMenu==mc.player.inventoryMenu)mc.setScreen(null);next=now+200;return;
                }
                command("first","task_submit",request(A,2));phase=2;
            }else if(phase==2){
                JsonObject result=result(A);if(!terminal(result))return;
                require(result.get("success").getAsBoolean()&&result.get("collected").getAsInt()>=2,"real collection failed: "+result);
                record("real_collect_two_logs",result);int before=logCount(snapshot());command("replay","task_submit",request(A,2));
                require(!((Boolean)busy.invoke(scheduler))&&logCount(snapshot())==before,"replay performed a new action");record("idempotent_replay",result);
                JsonObject coal=request(E,1);coal.addProperty("resource","coal");command("coal","task_submit",coal);phase=12;
            }else if(phase==12){
                JsonObject result=result(E);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"real coal collection failed: "+result);record("real_collect_coal",result);
                JsonObject iron=request(F,1);iron.addProperty("resource","iron");command("iron","task_submit",iron);phase=13;
            }else if(phase==13){
                JsonObject result=result(F);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"real iron collection failed: "+result);record("real_collect_iron",result);
                JsonObject wheat=request(G,1);wheat.addProperty("resource","wheat");command("wheat","task_submit",wheat);phase=14;
            }else if(phase==14){
                JsonObject result=result(G);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"real wheat collection failed: "+result);record("real_collect_wheat",result);
                command("second","task_submit",request(B,32));phase=3;
            }else if(phase==3){
                JsonObject result=result(B);String child=result.get("phase").getAsString();
                if(!Set.of("moving","ready_to_mine","mining").contains(child)){if(terminal(result))throw new IllegalStateException("second goal ended before stop: "+result);return;}
                command("stop","baritone",JsonParser.parseString("{command:'stop'}").getAsJsonObject());
                JsonObject stopped=result(B);require(stopped.get("phase").getAsString().equals("interrupted"),"stop did not interrupt parent");
                logsAtStop=logCount(snapshot());record("stop_interrupts_native_chain",stopped);phase=4;next=now+2000;
            }else if(phase==4){
                require(logCount(snapshot())==logsAtStop&&!((Boolean)busy.invoke(scheduler)),"action continued after stop");
                mc.getSingleplayerServer().execute(()->mc.getSingleplayerServer().overworld().setDayTime(13000));phase=5;next=now+1500;
            }else if(phase==5){
                command("night","task_submit",request(C,1));JsonObject reply=replies.get("night");
                require(reply!=null&&!reply.get("ok").getAsBoolean()&&!((Boolean)busy.invoke(scheduler)),"night goal should be rejected");
                record("night_submission_rejected",reply);
                mc.getSingleplayerServer().execute(()->{
                    var level=mc.getSingleplayerServer().overworld();level.setDayTime(6000);
                    var player=mc.getSingleplayerServer().getPlayerList().getPlayer(mc.player.getUUID());BlockPos at=player.blockPosition();
                    for(var direction:net.minecraft.core.Direction.Plane.HORIZONTAL)for(int dy=0;dy<=2;dy++)level.setBlock(at.relative(direction).above(dy),Blocks.OBSIDIAN.defaultBlockState(),3);
                });phase=6;next=now+1500;
            }else if(phase==6){command("blocked","task_submit",request(D,1));phase=7;
            }else if(phase==7){
                JsonObject result=result(D);if(!terminal(result))return;
                require(!result.get("success").getAsBoolean()&&result.get("collected").getAsInt()==0,"blocked goal must fail without claiming gain");
                record("unreachable_terrain_bounded_failure",result);
                setupProduction(mc);phase=20;next=now+1500;
            }else if(phase==20){command("planks","task_submit",production(H,"craft","minecraft:oak_planks",4));phase=21;
            }else if(phase==21){
                JsonObject result=result(H);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"local plank craft failed: "+result);record("local_inventory_crafting",result);
                command("pickaxe","task_submit",production(I,"craft","minecraft:wooden_pickaxe",1));phase=22;
            }else if(phase==22){
                JsonObject result=result(I);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"local table craft failed: "+result);record("local_owned_workbench_crafting",result);
                command("smelt","task_submit",production(J,"smelt","minecraft:charcoal",1));phase=23;
            }else if(phase==23){
                JsonObject result=result(J);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"local furnace failed: "+result);record("local_owned_furnace_smelt",result);
                command("stone","task_submit",request(K,1)); // resource selected explicitly below
                phase=24;
            }else if(phase==24){
                JsonObject result=result(K);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"exposed stone collection failed: "+result);record("local_exposed_stone",result);
                setupQuarry(mc);phase=25;next=now+1500;
            }else if(phase==25){command("quarry-"+quarryTrial,"task_submit",request(quarryTask,1));phase=26;
            }else if(phase==26){
                JsonObject result=result(quarryTask);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"shallow quarry failed: "+result);
                require(result.getAsJsonArray("new_supports").size()>0,"quarry support not journaled");quarryResult=result;record(quarryTrial==0?"local_shallow_quarry_and_journal":"local_shallow_quarry_repeat_"+(quarryTrial+1),result);
                if(++quarryTrial<3){quarryTask=String.format("%032x",256+quarryTrial);setupQuarry(mc);phase=25;next=now+1500;return;}
                setupTerrain(mc);phase=27;next=now+1500;
            }else if(phase==27){
                Class<?> terrain=Class.forName("com.astrbot.bridge.TerrainQueries");Method height=terrain.getDeclaredMethod("height",net.minecraft.client.player.LocalPlayer.class,BlockPos.class,Set.class);height.setAccessible(true);
                double slab=(Double)height.invoke(null,mc.player,new BlockPos(-8,65,12),Set.of()),stairs=(Double)height.invoke(null,mc.player,new BlockPos(-7,66,12),Set.of());
                double closed=(Double)height.invoke(null,mc.player,new BlockPos(12,65,-8),Set.of());
                require(Math.abs(slab-65.5)<.01&&Math.abs(stairs-66)<.01&&!Double.isFinite(closed),"collision proof for slab/stair/closed door invalid");
                JsonObject proof=new JsonObject();proof.addProperty("slab_feet",slab);proof.addProperty("stair_feet",stairs);proof.addProperty("closed_door_refused",true);record("collision_shape_queries",proof);
                command("terrain","task_submit",request(M,1));phase=28;
            }else if(phase==28){
                JsonObject result=result(M);if(!terminal(result))return;require(result.get("success").getAsBoolean(),"slab stair open-door route failed: "+result);
                JsonObject feedback=result.getAsJsonObject("executor_metrics").getAsJsonObject("path_feedback");require(feedback.get("supported").getAsBoolean()&&feedback.get("events").getAsInt()>0,"real Baritone path events not received");record("slab_stair_open_door_route_and_path_events",result);
                command("before-death","task_submit",request(N,32));deathAt=now;phase=29;
                mc.getSingleplayerServer().execute(()->{
                    var server=mc.getSingleplayerServer();var player=server.getPlayerList().getPlayer(mc.player.getUUID());
                    server.getGameRules().getRule(net.minecraft.world.level.GameRules.RULE_KEEPINVENTORY).set(true,server);
                    player.setRespawnPosition(net.minecraft.world.level.Level.OVERWORLD,new BlockPos(0,65,0),0,true,false);
                    player.hurt(player.damageSources().genericKill(),Float.MAX_VALUE);
                });
            }else if(phase==29){
                if(mc.player.isDeadOrDying()||now-deathAt<1000)return;
                require(!((Boolean)busy.invoke(scheduler))&&terminal(result(N)),"old task remained active after death");
                JsonObject status=snapshot();require(status.get("auto_respawn").getAsBoolean()&&status.get("health").getAsDouble()>0,"normal automatic respawn not confirmed");
                record("normal_automatic_respawn_cancels_old_goal",status);finish(true,"");mc.stop();
            }
            if(now-started>240000)throw new IllegalStateException("fixture deadline exceeded");
        }catch(Exception ex){Throwable error=ex;while(error instanceof java.lang.reflect.InvocationTargetException&&error.getCause()!=null)error=error.getCause();try{finish(false,error.toString());}catch(Exception ignored){}mc.stop();}
    }
    private static JsonObject snapshot()throws Exception{return (JsonObject)status.invoke(bridge,Minecraft.getInstance().player);}
    private static JsonObject request(String id,int count)throws Exception{
        JsonObject s=snapshot(),scope=s.getAsJsonObject("position").deepCopy();scope.add("world_key",s.get("world_key"));scope.add("dimension",s.get("dimension"));
        // The first scope is retained in replay rather than recomputed after walking.
        if(id.equals(A)&&replies.containsKey("first"))return replies.get("first-request").deepCopy();
        JsonObject p=new JsonObject();p.addProperty("task_id",id);p.addProperty("kind","collect");p.addProperty("resource","logs");p.addProperty("count",count);p.addProperty("radius",24);p.addProperty("seconds",60);p.add("scope",scope);p.add("protected_supports",new JsonArray());
        if(id.equals(K)||id.equals(quarryTask))p.addProperty("resource","stone");
        if(id.equals(A))replies.put("first-request",p.deepCopy());return p;
    }
    private static JsonObject production(String id,String kind,String item,int count)throws Exception{
        JsonObject p=request(id,count);p.addProperty("kind",kind);p.addProperty("resource",item);p.addProperty("seconds",180);
        JsonObject table=p.getAsJsonObject("scope").deepCopy();table.addProperty("x",-2);table.addProperty("y",65);table.addProperty("z",2);
        JsonArray tables=new JsonArray();tables.add(table);p.add("owned_tables",tables);
        JsonObject furnace=p.getAsJsonObject("scope").deepCopy();furnace.addProperty("x",2);furnace.addProperty("y",65);furnace.addProperty("z",2);furnace.addProperty("id","minecraft:furnace");
        JsonArray stations=new JsonArray();stations.add(furnace);p.add("owned_stations",stations);if(kind.equals("smelt"))p.add("station",furnace.deepCopy());return p;
    }
    private static void setupProduction(Minecraft mc){
        mc.getSingleplayerServer().execute(()->{var server=mc.getSingleplayerServer();var player=server.getPlayerList().getPlayer(mc.player.getUUID());var level=server.overworld();BlockPos at=player.blockPosition();
            for(var direction:net.minecraft.core.Direction.Plane.HORIZONTAL)for(int dy=0;dy<=2;dy++)level.setBlock(at.relative(direction).above(dy),Blocks.AIR.defaultBlockState(),3);
            player.teleportTo(level,.5,65,.5,0,0);player.getFoodData().setFoodLevel(20);
            player.getInventory().setItem(25,new net.minecraft.world.item.ItemStack(net.minecraft.world.item.Items.OAK_PLANKS,8));
            player.getInventory().setItem(26,new net.minecraft.world.item.ItemStack(net.minecraft.world.item.Items.COBBLESTONE,8));
            player.getInventory().setItem(27,new net.minecraft.world.item.ItemStack(net.minecraft.world.item.Items.STICK,8));
            level.setBlock(new BlockPos(2,65,2),Blocks.FURNACE.defaultBlockState(),3);player.inventoryMenu.broadcastChanges();
        });
    }
    private static void setupQuarry(Minecraft mc){
        mc.getSingleplayerServer().execute(()->{var level=mc.getSingleplayerServer().overworld();for(int z=4;z<=9;z++)for(int x=8;x<=9;x++)level.setBlock(new BlockPos(x,65,z),Blocks.AIR.defaultBlockState(),3);
            for(int x=-9;x<=-6;x++)for(int z=-7;z<=-4;z++){level.setBlock(new BlockPos(x,64,z),Blocks.GRASS_BLOCK.defaultBlockState(),3);for(int y=62;y<=63;y++)level.setBlock(new BlockPos(x,y,z),Blocks.STONE.defaultBlockState(),3);}
            var player=mc.getSingleplayerServer().getPlayerList().getPlayer(mc.player.getUUID());player.teleportTo(level,-4.5,65,-5.5,0,0);
        });
    }
    private static void setupTerrain(Minecraft mc){
        mc.getSingleplayerServer().execute(()->{var server=mc.getSingleplayerServer();var player=server.getPlayerList().getPlayer(mc.player.getUUID());var level=server.overworld();
            for(int x=-11;x<=-2;x++)for(int z=11;z<=13;z++)for(int y=65;y<=71;y++)level.setBlock(new BlockPos(x,y,z),Blocks.AIR.defaultBlockState(),3);
            for(int x=-10;x<=-3;x++)for(int z:new int[]{11,13})for(int y=65;y<=68;y++)level.setBlock(new BlockPos(x,y,z),Blocks.GLASS.defaultBlockState(),3);
            level.setBlock(new BlockPos(-8,65,12),Blocks.SMOOTH_STONE_SLAB.defaultBlockState(),3);
            level.setBlock(new BlockPos(-7,65,12),Blocks.STONE_STAIRS.defaultBlockState().setValue(net.minecraft.world.level.block.StairBlock.FACING,net.minecraft.core.Direction.EAST),3);
            for(int x=-6;x<=-3;x++)level.setBlock(new BlockPos(x,65,12),Blocks.DIRT.defaultBlockState(),3);
            var open=Blocks.OAK_DOOR.defaultBlockState().setValue(net.minecraft.world.level.block.DoorBlock.FACING,net.minecraft.core.Direction.EAST).setValue(net.minecraft.world.level.block.DoorBlock.OPEN,true);
            level.setBlock(new BlockPos(-5,66,12),open,3);level.setBlock(new BlockPos(-5,67,12),open.setValue(net.minecraft.world.level.block.DoorBlock.HALF,net.minecraft.world.level.block.state.properties.DoubleBlockHalf.UPPER),3);
            level.setBlock(new BlockPos(12,65,-8),Blocks.OAK_DOOR.defaultBlockState(),3);level.setBlock(new BlockPos(12,66,-8),Blocks.OAK_DOOR.defaultBlockState().setValue(net.minecraft.world.level.block.DoorBlock.HALF,net.minecraft.world.level.block.state.properties.DoubleBlockHalf.UPPER),3);
            for(int y=66;y<=69;y++)level.setBlock(new BlockPos(-3,y,12),Blocks.OAK_LOG.defaultBlockState(),3);
            for(BlockPos leaf:BlockPos.betweenClosed(-5,69,10,-1,70,14))if(!level.getBlockState(leaf).is(Blocks.OAK_LOG))level.setBlock(leaf,Blocks.OAK_LEAVES.defaultBlockState(),3);
            player.teleportTo(level,-10.5,65,12.5,0,0);player.getFoodData().setFoodLevel(20);
        });
    }
    private static void command(String id,String action,JsonObject p)throws Exception{JsonObject message=new JsonObject();message.addProperty("id",id);message.addProperty("action",action);message.add("params",p);handle.invoke(bridge,message);JsonObject response=replies.get(id);if(action.equals("task_submit")&&!id.equals("night")&&response!=null&&!response.get("ok").getAsBoolean())throw new IllegalStateException("submit "+id+" rejected: "+response);}
    private static JsonObject result(String id)throws Exception{return (JsonObject)taskStatus.invoke(scheduler,id);}
    private static boolean terminal(JsonObject p){return Set.of("completed","failed","interrupted").contains(p.get("phase").getAsString());}
    private static int logCount(JsonObject s){int n=0;for(var e:s.getAsJsonArray("inventory_items")){JsonObject i=e.getAsJsonObject();if(i.get("slot").getAsInt()<36&&i.get("id").getAsString().endsWith("_log"))n+=i.get("count").getAsInt();}return n;}
    private static void require(boolean value,String message){if(!value)throw new IllegalStateException(message);}
    private static void record(String name,JsonObject detail){JsonObject r=new JsonObject();r.addProperty("name",name);r.addProperty("ok",true);r.add("detail",detail.deepCopy());cases.add(r);}
    private static JsonObject debugQuarry()throws Exception{
        Minecraft mc=Minecraft.getInstance();JsonObject out=new JsonObject();out.add("actor",snapshot());
        Class<?> terrain=Class.forName("com.astrbot.bridge.TerrainQueries");Method height=terrain.getDeclaredMethod("height",net.minecraft.client.player.LocalPlayer.class,BlockPos.class,Set.class);height.setAccessible(true);
        BlockPos work=new BlockPos(-6,63,-7);out.addProperty("height_after_removal",(Double)height.invoke(null,mc.player,work,Set.of(work)));
        Field envField=scheduler.getClass().getDeclaredField("env");envField.setAccessible(true);Object env=envField.get(scheduler);
        Method reachable=env.getClass().getDeclaredMethod("reachable",JsonObject.class,JsonArray.class,JsonObject.class,double.class);reachable.setAccessible(true);
        JsonObject point=new JsonObject();point.addProperty("x",-6);point.addProperty("y",63);point.addProperty("z",-7);JsonArray ignored=new JsonArray();ignored.add(point.deepCopy());
        JsonObject scope=snapshot().getAsJsonObject("position").deepCopy();out.addProperty("reachable_after_removal",(Boolean)reachable.invoke(env,point,ignored,scope,24.0));
        point.addProperty("resource","stone");point.addProperty("target_only",true);command("quarry-debug","scan",point);out.add("scan",replies.get("quarry-debug").deepCopy());return out;
    }
    private static void finish(boolean ok,String error)throws Exception{
        ended=true;JsonObject r=new JsonObject();r.addProperty("ok",ok);r.addProperty("world","Codex-LocalScheduler-20261102");r.addProperty("cloud_connected",false);r.addProperty("qq_messages_sent",0);r.add("cases",cases);r.addProperty("error",error);r.addProperty("events",events.size());r.addProperty("elapsed_ms",System.currentTimeMillis()-started);
        if(!ok){JsonArray trace=new JsonArray();for(int i=Math.max(0,events.size()-80);i<events.size();i++)trace.add(events.get(i));r.add("failure_trace",trace);}
        if(!ok&&phase==26)try{r.add("quarry_debug",debugQuarry());}catch(Exception diagnostic){r.addProperty("debug_error",diagnostic.toString());}
        Files.createDirectories(output);Files.writeString(output.resolve("local-scheduler-result.json"),r.toString());
    }
}
