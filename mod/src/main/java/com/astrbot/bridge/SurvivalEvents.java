package com.astrbot.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.MobCategory;

/** Lightweight edge-triggered survival events. No positions leave the client. */
final class SurvivalEvents {
    private int lastHealth = -1;
    private int lastFood = -1;
    private int lastHostileCount = -1;
    private String lastHostileKey = "";
    private boolean lastDead = false;
    private long lastEmit = 0L;
    private long lastHostileAt = 0L;
    private long seq = 0L;

    void reset() {
        lastHealth = -1;
        lastFood = -1;
        lastHostileCount = -1;
        lastHostileKey = "";
        lastDead = false;
        lastEmit = 0L;
        lastHostileAt = 0L;
    }

    JsonObject tick(LocalPlayer player) {
        if (player == null) {
            reset();
            return null;
        }
        long now = System.currentTimeMillis();
        int health = (int) Math.ceil(player.getHealth());
        int food = player.getFoodData().getFoodLevel();
        boolean dead = player.isDeadOrDying();
        List<LivingEntity> mobs = new ArrayList<>();
        player.level()
                .getEntities(
                        player,
                        player.getBoundingBox().inflate(10.0),
                        entity -> entity != player
                                && entity instanceof LivingEntity
                                && entity.getType().getCategory() == MobCategory.MONSTER)
                .forEach(entity -> {
                    if (mobs.size() < 8) mobs.add((LivingEntity) entity);
                });
        mobs.sort(Comparator.comparingDouble(entity -> entity.distanceTo(player)));
        String hostileKey = hostileKey(mobs, player);

        boolean emit = false;
        String kind = "";
        if (dead && !lastDead) {
            kind = "death";
            emit = true;
        } else if (!dead && SurvivalRules.damageEvent(health, lastHealth, now, lastEmit)) {
            kind = "damage";
            emit = true;
        } else if (!dead && SurvivalRules.foodEvent(food, lastFood, now, lastEmit)) {
            kind = "food";
            emit = true;
        } else if (!dead && SurvivalRules.hostileEvent(
                mobs.size(), lastHostileCount, hostileKey, lastHostileKey, now, lastHostileAt)) {
            kind = mobs.isEmpty() ? "clear" : "hostile";
            emit = true;
        } else if (!dead && health != lastHealth && now - lastEmit >= 1000
                && (health <= 14 || lastHealth <= 14)) {
            kind = "health";
            emit = true;
        }

        JsonObject data = null;
        if (emit) {
            seq++;
            data = new JsonObject();
            data.addProperty("seq", seq);
            data.addProperty("kind", kind);
            data.addProperty("at", now);
            data.addProperty("health", player.getHealth());
            data.addProperty("max_health", player.getMaxHealth());
            data.addProperty("food", food);
            data.addProperty("saturation", player.getFoodData().getSaturationLevel());
            data.addProperty("dead", dead);
            JsonArray hostiles = new JsonArray();
            for (LivingEntity mob : mobs) {
                JsonObject row = new JsonObject();
                row.addProperty("type", net.minecraft.core.registries.BuiltInRegistries.ENTITY_TYPE
                        .getKey(mob.getType()).toString());
                row.addProperty("name", mob.getName().getString());
                row.addProperty("distance", mob.distanceTo(player));
                row.addProperty("line_of_sight", player.hasLineOfSight(mob));
                row.addProperty("health", mob.getHealth());
                hostiles.add(row);
            }
            data.add("hostiles", hostiles);
            lastEmit = now;
            if (!mobs.isEmpty() || "clear".equals(kind)) lastHostileAt = now;
        }
        lastHealth = health;
        lastFood = food;
        lastHostileCount = mobs.size();
        lastHostileKey = hostileKey;
        lastDead = dead;
        return data;
    }

    private static String hostileKey(List<LivingEntity> mobs, LocalPlayer player) {
        if (mobs.isEmpty()) return "";
        LivingEntity nearest = mobs.get(0);
        int bucket = (int) Math.max(0, Math.min(3, nearest.distanceTo(player) / 3.0));
        return mobs.size() + ":" + bucket + ":" + nearest.getType();
    }
}
