/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package core;

import java.util.Date;
import java.util.List;
import java.util.Random;
import java.util.logging.Level;
import java.util.logging.Logger;
import net.MessageTransport;
import ses.EggyRush;
import sync.SinghalHeuristic;

/**
 *
 * @author Creativ
 */
public class EggGenerator extends Thread {

    private static final int MAX = Math.max(5, 2 * EggyRush.PlayNum);
    int CurrentEggNum = MAX;
    private List<Egg> EggCollection;
    private int[] maze;
    private int[] FootprintMap;
    private int[] EggMap;
    private SinghalHeuristic EggGenSync;
    private MessageTransport messageTransport;

    public void reference(MessageTransport mt) {
        this.messageTransport = mt;
    }

    public void reference(int[] maze, int[] fpMap, int[] eggMap) {
        this.maze = maze;
        this.FootprintMap = fpMap;
        this.EggMap = eggMap;
    }

    public void reference(List<Egg> eggCollection) {
        this.EggCollection = eggCollection;
    }

    public void reference(SinghalHeuristic egs) {
        this.EggGenSync = egs;
    }

    public void prompt() {
        CurrentEggNum--;
        synchronized (EggMap) {
            EggMap.notify();
        }
    }

    @Override
    public void run() {
        while (true) {
            synchronized (EggMap) {
                try {
                    EggMap.wait();
                } catch (InterruptedException ex) {
                    Logger.getLogger(EggGenerator.class.getName()).log(Level.SEVERE, null, ex);
                }
            }

            EggGenSync.request();

            Random randgen = new Random(System.currentTimeMillis());
            while (true) {
                int rn = randgen.nextInt();
                int i = Math.abs(rn) % (639); // 639 = 32* 20 - 1
                if (maze[i] != TheMatrix.WALL) {
                    if (EggMap[i] == TheMatrix.NO_EGG) {
                        synchronized (FootprintMap) {
                            if (FootprintMap[i] == TheMatrix.NO_FOOTPRINT) {
                                Egg newEgg = new Egg(i / 32, i % 32);
                                EggCollection.add(newEgg);
                                EggMap[i] = EggCollection.size() - 1;
                                messageTransport.broadcast(newEgg);
                                CurrentEggNum++;
                                break;
                            }
                        }
                    }
                }
            }

            EggGenSync.release();
        }
    }
}
