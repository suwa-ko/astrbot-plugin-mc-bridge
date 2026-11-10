package com.astrbot.bridge;

/** Small, independently testable protocol limits. Inventory indices are vanilla 0-based. */
final class SurvivalRules {
    private SurvivalRules() {}
    static String itemId(String value) {
        if (value == null || !value.matches("(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*"))
            throw new IllegalArgumentException("invalid registry id");
        return value.contains(":") ? value : "minecraft:" + value;
    }
    static boolean inventorySlot(int slot) { return slot >= 0 && slot < 36; }
    static boolean hotbarSlot(int slot) { return slot >= 0 && slot < 9; }
    static boolean within(double x, double y, double z, double ax, double ay, double az, double radius) {
        return Double.isFinite(x+y+z+ax+ay+az+radius) && radius >= 0 && radius <= 64
                && Math.abs(y-ay) <= 12 && Math.hypot(x-ax,z-az) <= radius;
    }
    static boolean night(long dayTime) {
        long t = Math.floorMod(dayTime, 24000L);
        return t >= 12542 && t < 23460;
    }
    static boolean sameBuildLevel(int blockY,double feetY) { return Double.isFinite(feetY)&&Math.abs(blockY-feetY)<=0.75; }
    static boolean safeMiningCell(int x,int y,int z,double px,double py,double pz) {
        if(!Double.isFinite(px+py+pz)||y<Math.floor(py)-2)return false;
        boolean supportsFeet=y<py+.1&&y+1>py-.25&&x<px+.45&&x+1>px-.45&&z<pz+.45&&z+1>pz-.45;
        return !supportsFeet;
    }
    static boolean safeUnderfootMiningCell(int x,int y,int z,double px,double py,double pz) {
        if(!Double.isFinite(px+py+pz))return false;
        int feet=(int)Math.floor(py);
        if(y!=feet-1)return false;
        return x<px+.45&&x+1>px-.45&&z<pz+.45&&z+1>pz-.45;
    }
    static int checkedCount(int count) {
        if (count < 1 || count > 64) throw new IllegalArgumentException("count must be 1..64");
        return count;
    }
    static boolean keepsAccess(int before,int after) {
        return before>0&&after>=Math.min(before,12)&&after*5>=before*4;
    }
    static int craftSlot(int grid,int width,int ingredient) {
        if((grid!=2&&grid!=3)||width<1||width>grid||ingredient<0||ingredient>=width*grid)throw new IllegalArgumentException("invalid crafting dimensions");
        return 1+(ingredient/width)*grid+ingredient%width;
    }
    static boolean damageEvent(int health,int previous,long now,long lastEmit) {
        return health>=0&&previous>=0&&health<previous&&now-lastEmit>=250;
    }
    static boolean foodEvent(int food,int previous,long now,long lastEmit) {
        return food>=0&&previous>=0&&food!=previous&&(food<=10||previous<=10)&&now-lastEmit>=1000;
    }
    static boolean hostileEvent(int count,int previous,String key,String lastKey,long now,long lastAt) {
        if(count<0||key==null||lastKey==null)return false;
        if(count==0)return previous>0;
        return previous==0||!key.equals(lastKey)||now-lastAt>=1000;
    }
}
