/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

import org.xml.sax.helpers.DefaultHandler;



/**
 *
 * @author Creativ
 */
public class Pair extends DefaultHandler implements Comparable<Pair> {
    int pid;
    int[] t;


    public synchronized void update(Pair PairInVM){
        for(int i=0; i<this.t.length; i++){
            this.t[i] = Math.max(this.t[i], PairInVM.getTimeValue()[i]);
        }
    }

    public Pair() {
    }

    
    public Pair(int id, int[] time) {
        this.pid = id;
        this.t = time;
    }
    
    public int getID(){
        return this.pid;
    }

    @Override
    public String toString() {
        String xmlstr =  "<pair>" + 
                "<pid>" + pid + "</pid>" +
                "<timestamp>" + java.util.Arrays.toString(t) + "</timestamp>"+
                "</pair>";
        return xmlstr;
    }
    
    public void setID(int pid){
        this.pid = pid;
    }
    
    public synchronized void setTime(int[] time){
        this.t = time;
    }

    @Override
    public int compareTo(Pair o) {
        return this.pid - o.getID();
    }
    
    public int[] getTimeValue(){
        return this.t;
    }
    
    public TimeStamp getTimeStamp(){
        return new TimeStamp(this.t);
    }
    
    public String toLog(){
        return "\t\t(" + this.pid + ", " + java.util.Arrays.toString(t) + ")" + System.getProperty("line.separator");
    }
}
