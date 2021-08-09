/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package core;

import java.util.Iterator;
import java.util.List;
import java.util.logging.Level;
import java.util.logging.Logger;

/**
 *
 * @author Creativ
 */
public class Feeder extends Thread {

    private List<Egg> EggCollection;
    
    public void reference(List<Egg> EggCollection){
        this.EggCollection = EggCollection;
    }
    @Override
    public void run() {
        while(true){
            try {
                Thread.sleep(1000);
            } catch (InterruptedException ex) {
                Logger.getLogger(Feeder.class.getName()).log(Level.SEVERE, null, ex);
            }
            
            Iterator<Egg> iter = EggCollection.iterator();
            while(iter.hasNext()){
                Egg e = iter.next();
                if(e.Phase() < 4){
                    e.PhaseUp();
                }
            }
        }
    }
    
    
}
