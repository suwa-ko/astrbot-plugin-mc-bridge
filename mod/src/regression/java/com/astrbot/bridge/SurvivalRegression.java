package com.astrbot.bridge;

/** Protocol boundaries run without opening Minecraft or sending packets. */
public final class SurvivalRegression {
    private static void require(boolean value,String label) { if(!value)throw new AssertionError(label); }
    public static void main(String[] args) {
        require(SurvivalRules.itemId("oak_log").equals("minecraft:oak_log"),"normal item");
        require(SurvivalRules.itemId("mod:item").equals("mod:item"),"mod item");
        for(String invalid:new String[]{"/give","oak_log;stop","minecraft:stone\n#stop","","a:b:c"}) {
            try { SurvivalRules.itemId(invalid); throw new AssertionError("accepted "+invalid); }
            catch(IllegalArgumentException expected) {}
        }
        require(!SurvivalRules.inventorySlot(-1)&&SurvivalRules.inventorySlot(35)&&!SurvivalRules.inventorySlot(36),"inventory limits");
        require(!SurvivalRules.hotbarSlot(-1)&&SurvivalRules.hotbarSlot(8)&&!SurvivalRules.hotbarSlot(9),"hotbar limits");
        require(SurvivalRules.within(3,64,4,0,64,0,5),"bounded move");
        require(!SurvivalRules.within(6,64,0,0,64,0,5),"outside range");
        require(!SurvivalRules.within(Double.NaN,64,0,0,64,0,5),"nonfinite move");
        require(!SurvivalRules.within(0,90,0,0,64,0,5),"vertical range");
        require(!SurvivalRules.within(0,64,0,0,64,0,100),"oversized scope");
        require(!SurvivalRules.night(0)&&SurvivalRules.night(13000)&&SurvivalRules.night(37000)&&!SurvivalRules.night(23460),"night phase");
        for(int invalid:new int[]{-1,0,65,Integer.MAX_VALUE}) {
            try { SurvivalRules.checkedCount(invalid);throw new AssertionError("accepted count"); }
            catch(IllegalArgumentException expected) {}
        }
        require(SurvivalRules.checkedCount(64)==64,"craft quantity cap");
        require(SurvivalRules.sameBuildLevel(65,64.9375),"farmland does not hide nearby build sites");
        require(SurvivalRules.sameBuildLevel(65,65.0)&&SurvivalRules.sameBuildLevel(65,64.5),"full block and slab build levels");
        require(!SurvivalRules.sameBuildLevel(66,64.9375),"no second-storey placement from farmland");
        require(!SurvivalRules.safeMiningCell(0,63,0,.5,64,.5),"never dig the supporting block");
        require(!SurvivalRules.safeMiningCell(1,63,0,.95,64,.5),"protect footprint at block edges");
        require(SurvivalRules.safeMiningCell(1,63,0,.5,64,.5),"visible adjacent lower stone may be mined");
        require(SurvivalRules.safeMiningCell(2,62,0,.5,64,.5),"bounded two-block side depth");
        require(!SurvivalRules.safeMiningCell(2,61,0,.5,64,.5),"no uncontrolled downward excavation");
        require(!SurvivalRules.safeMiningCell(2,64,0,Double.NaN,64,.5),"nonfinite player rejected");
        require(SurvivalRules.safeUnderfootMiningCell(0,63,0,.5,64,.5),"exact block under feet is eligible for guarded excavation");
        require(!SurvivalRules.safeUnderfootMiningCell(1,63,0,.5,64,.5),"adjacent under-feet block is not eligible");
        require(!SurvivalRules.safeUnderfootMiningCell(0,62,0,.5,64,.5),"two blocks below feet is not eligible");
        System.out.println("Survival protocol boundary regression passed");
        require(!SurvivalRules.keepsAccess(40,2),"never seal a corridor around the player");
        require(!SurvivalRules.keepsAccess(40,15),"do not isolate a small room");
        require(SurvivalRules.keepsAccess(40,39),"ordinary wall-side station keeps access");
        require(!SurvivalRules.keepsAccess(3,2),"do not further restrict an existing tight pocket");
        require(SurvivalRules.craftSlot(2,1,0)==1&&SurvivalRules.craftSlot(2,1,1)==3,"vertical torch recipe in personal grid");
        require(SurvivalRules.craftSlot(3,2,3)==5,"shaped recipe width preserves empty columns");
        require(SurvivalRules.craftSlot(3,3,8)==9,"last crafting table cell");
        try{SurvivalRules.craftSlot(2,3,0);throw new AssertionError("oversize recipe accepted");}catch(IllegalArgumentException expected){}
        require(SurvivalRules.damageEvent(12,18,1000,0),"damage trigger");
        require(!SurvivalRules.damageEvent(12,18,100,0),"damage throttle");
        require(!SurvivalRules.damageEvent(18,12,1000,0),"heal is not damage");
        require(SurvivalRules.foodEvent(7,8,1000,0)&&SurvivalRules.foodEvent(9,8,1000,0),"food threshold change");
        require(!SurvivalRules.foodEvent(19,20,2000,0),"food change outside threshold");
        require(SurvivalRules.hostileEvent(1,0,"1:0:zombie","",1000,0),"first hostile");
        require(!SurvivalRules.hostileEvent(1,1,"1:0:zombie","1:0:zombie",1500,1000),"hostile throttle");
        require(SurvivalRules.hostileEvent(0,1,"","1:0:zombie",2000,1000),"hostile clear");
        require(!SurvivalRules.hostileType("genshin_nature:luminousseelie","genshin_nature:luminousseelie"),"modded passive entity excluded");
        require(SurvivalRules.hostileType("minecraft:zombie","genshin_nature:luminousseelie"),"zombie remains hostile");
        require(SurvivalRules.hostileType("minecraft:zombie",""),"empty ignore list keeps hostile");
    }
}
