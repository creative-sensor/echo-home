/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package device;

import entity.Mp3item;
import java.util.Iterator;
import java.util.LinkedList;
import java.util.List;

/**
 *
 * @author creativ
 */
public class FlashPlayer {

    public static int MODE_SINGLE = 0;
    public static int MODE_MULTI = 1;
    private String headPart;
    private String tailPart;
    private String dataPart;
    private String playList;
    private String titleList = "&amp;title=";

    public FlashPlayer(int mode) {
        switch (mode) {
            case 0: // single mode
                headPart = "<object type=\"application/x-shockwave-flash\" data=\"" + Config.fqdn + "Widgets/player_mp3_maxi.swf\" width=\"600\" height=\"30\">\n"
                        + "    <param name=\"movie\" value=\"" + Config.fqdn + "Widgets/player_mp3_maxi.swf\" />\n"
                        + "    <param name=\"bgcolor\" value=\"#ffffff\" />\n"
                        + "    <param name=\"FlashVars\" value=\"mp3=";
                tailPart = "&amp;width=600&amp;height=30&amp;loop=1&amp;showvolume=1&amp;volumewidth=80&amp;volumeheight=10\" />\n"
                        + "</object>";
                break;
            case 1: // multi mode
                headPart = "<object type=\"application/x-shockwave-flash\" data=\"" + Config.fqdn + "Widgets/player_mp3_multi.swf\" width=\"400\" height=\"300\">\n"
                        + "    <param name=\"movie\" value=\"" + Config.fqdn + "Widgets/player_mp3_multi.swf\" />\n"
                        + "    <param name=\"bgcolor\" value=\"#ffffff\" />\n"
                        + "    <param name=\"FlashVars\" value=\"mp3=";
                tailPart = "&amp;width=400&amp;height=300&amp;loop=1&amp;showvolume=1&amp;volumewidth=80&amp;volumeheight=10&amp;autoplay=1&amp;bgcolor1=003D5C\" />\n"
                        + "</object>";

        }
    }

    public void setPlayList(List<Mp3item> mp3list) {
        reset();
        if (mp3list.isEmpty()) {
            return;
        }
        Iterator<Mp3item> iter = mp3list.iterator();
        Mp3item temp = iter.next();
        playList = Config.fqdn + "GetItem?id=" + temp.getId();
        titleList = titleList.concat(temp.getName() + " - " + temp.getArtist());
        while (iter.hasNext()) {
            Mp3item item = iter.next();
            playList = playList.concat("|" + Config.fqdn + "GetItem?id=" + item.getId());
            titleList = titleList.concat("|" + item.getName() + " - " + item.getArtist());

        }
    }

    private void reset() {
        playList = "";
        titleList = "&amp;title=";
    }

    public String getHTML() {
        return this.headPart + this.playList + this.titleList + this.tailPart;
    }
    
    public String getDefaultHTML(){
        reset();
        this.titleList = this.titleList.concat("Home - Michael Bubble");
        this.playList = "http://data5.chiasenhac.com/downloads/1003/0/1002845-08e27a72/128/Home%20-%20Michael%20Buble%20%5BMP3%20128kbps%5D.mp3";
                
        return getHTML();
    }
    
}
