package com.astrbot.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.Comparator;
import java.util.ArrayList;
import java.util.Locale;
import java.util.HashMap;
import java.util.Map;
import java.util.TreeSet;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.AbstractCookingRecipe;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.ShapedRecipe;
import net.neoforged.fml.ModList;

/** Read server-synchronized recipes and client registries, never guessed wiki data. */
final class GameKnowledge {
    private GameKnowledge() {}
    static JsonObject query(LocalPlayer player,JsonObject request) {
        String q=request.has("query")?request.get("query").getAsString().strip().toLowerCase(Locale.ROOT):"";
        if(q.length()>80)throw new IllegalArgumentException("query exceeds 80 characters");
        String kind=request.has("kind")?request.get("kind").getAsString():"recipes";
        int limit=request.has("limit")?Math.max(1,Math.min(10,request.get("limit").getAsInt())):5;
        JsonObject result=new JsonObject();JsonArray rows=new JsonArray();int total=0;
        result.addProperty("kind",kind);
        if(kind.equals("mods")) {
            result.addProperty("source","loaded client mods, not a claim about server-only mods");
            Map<String,Integer> items=new HashMap<>(),recipes=new HashMap<>(),crafting=new HashMap<>();Map<String,TreeSet<String>> types=new HashMap<>();
            for(var key:BuiltInRegistries.ITEM.keySet())items.merge(key.getNamespace(),1,Integer::sum);
            for(var holder:player.level().getRecipeManager().getRecipes()) {
                String namespace=holder.id().getNamespace();recipes.merge(namespace,1,Integer::sum);
                if(holder.value() instanceof CraftingRecipe)crafting.merge(namespace,1,Integer::sum);
                types.computeIfAbsent(namespace,k->new TreeSet<>()).add(recipeType(holder));
            }
            var mods=new ArrayList<>(ModList.get().getMods());
            mods.sort(Comparator.comparingInt((net.neoforged.neoforgespi.language.IModInfo mod)->-recipes.getOrDefault(mod.getModId(),0)).thenComparing(mod->mod.getModId()));
            for(var mod:mods) {
                if(!matches(q,mod.getModId(),mod.getDisplayName()))continue;
                if(q.isEmpty()&&items.getOrDefault(mod.getModId(),0)==0&&recipes.getOrDefault(mod.getModId(),0)==0)continue;
                if(total++>=limit)continue;
                JsonObject row=new JsonObject();row.addProperty("id",mod.getModId());row.addProperty("name",mod.getDisplayName());row.addProperty("version",mod.getVersion().toString());
                row.addProperty("registered_items",items.getOrDefault(mod.getModId(),0));row.addProperty("server_recipes",recipes.getOrDefault(mod.getModId(),0));row.addProperty("server_crafting_recipes",crafting.getOrDefault(mod.getModId(),0));
                JsonArray recipeTypes=new JsonArray();for(String type:types.getOrDefault(mod.getModId(),new TreeSet<>()))if(recipeTypes.size()<10)recipeTypes.add(type);row.add("recipe_types",recipeTypes);rows.add(row);
            }
        } else if(kind.equals("items")) {
            result.addProperty("source","current client registry and localization");
            for(var item:BuiltInRegistries.ITEM) {
                ItemStack stack=item.getDefaultInstance();if(stack.isEmpty())continue;
                String key=BuiltInRegistries.ITEM.getKey(item).toString();
                if(!matches(q,key,stack.getHoverName().getString()))continue;
                if(total++<limit) {
                    JsonObject row=SurvivalActions.stackInfo(stack);JsonArray tooltip=new JsonArray();
                    try{for(var line:stack.getTooltipLines(net.minecraft.world.item.Item.TooltipContext.of(player.level()),player,net.minecraft.world.item.TooltipFlag.NORMAL))if(tooltip.size()<8)tooltip.add(line.getString().substring(0,Math.min(200,line.getString().length())));}catch(RuntimeException unsupported){}
                    row.add("description",tooltip);rows.add(row);
                }
            }
        } else if(kind.equals("recipes")) {
            result.addProperty("source","recipes synchronized by the currently connected server; custom crafting execution may need its own station");
            for(RecipeHolder<?> holder:player.level().getRecipeManager().getRecipes().stream().sorted(Comparator.comparingInt((RecipeHolder<?> r)->recipeRank(player,r,q)).thenComparing(r->r.id().toString())).toList()) {
                ItemStack output;
                try{output=holder.value().getResultItem(player.registryAccess());}catch(RuntimeException unsupported){continue;}
                if(output.isEmpty())continue;
                String key=BuiltInRegistries.ITEM.getKey(output.getItem()).toString();
                if(!matches(q,key,output.getHoverName().getString(),holder.id().toString()))continue;
                if(total++>=limit)continue;
                JsonObject row=new JsonObject();row.addProperty("recipe",holder.id().toString());row.add("output",SurvivalActions.stackInfo(output));
                row.addProperty("type",recipeType(holder));
                row.addProperty("unlocked",player.getRecipeBook().contains(holder));
                row.addProperty("bridge_crafting_supported",holder.value() instanceof CraftingRecipe);
                if(holder.value() instanceof ShapedRecipe shaped){row.addProperty("width",shaped.getWidth());row.addProperty("height",shaped.getHeight());}
                if(holder.value() instanceof AbstractCookingRecipe cooking)row.addProperty("cooking_ticks",cooking.getCookingTime());
                JsonArray ingredients=new JsonArray();
                for(var ingredient:holder.value().getIngredients()) {
                    if(ingredient.isEmpty())continue;
                    JsonObject group=new JsonObject();JsonArray choices=new JsonArray();var all=ingredient.getItems();
                    for(int i=0;i<Math.min(8,all.length);i++)choices.add(SurvivalActions.stackInfo(all[i]));
                    group.add("alternatives",choices);group.addProperty("alternative_count",all.length);ingredients.add(group);
                    if(ingredients.size()>=16)break;
                }
                row.add("ingredients",ingredients);rows.add(row);
            }
        } else throw new IllegalArgumentException("kind must be mods/items/recipes");
        result.add("results",rows);result.addProperty("matches",total);result.addProperty("truncated",total>rows.size());return result;
    }
    private static boolean matches(String query,String... values) {
        for(String value:values)if(value.toLowerCase(Locale.ROOT).contains(query))return true;
        return false;
    }
    private static String recipeType(RecipeHolder<?> holder) {
        // Some synchronized guide/display recipes use a non-registered type.
        // Keep those explicitly unknown instead of losing every other mod row.
        try{var key=BuiltInRegistries.RECIPE_TYPE.getKey(holder.value().getType());return key==null?"unregistered":key.toString();}
        catch(RuntimeException unsupported){return "unavailable";}
    }
    private static int recipeRank(LocalPlayer player,RecipeHolder<?> holder,String q) {
        try{var stack=holder.value().getResultItem(player.registryAccess());var key=BuiltInRegistries.ITEM.getKey(stack.getItem());if(key!=null&&(key.toString().equals(q)||key.getPath().equals(q)||stack.getHoverName().getString().toLowerCase(Locale.ROOT).equals(q)))return 0;}
        catch(RuntimeException unsupported){}
        return 1;
    }
}
